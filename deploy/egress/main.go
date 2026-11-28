package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	upstreamTimeout = 10 * time.Second
	tunnelTimeout   = 5 * time.Minute
)

var dnsLabel = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

// These special-use ranges also include Docker/LAN, loopback and metadata endpoints.
var nonPublicIPv4 = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("10.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("127.0.0.0/8"),
	netip.MustParsePrefix("169.254.0.0/16"),
	netip.MustParsePrefix("172.16.0.0/12"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("192.88.99.0/24"),
	netip.MustParsePrefix("192.168.0.0/16"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"),
	netip.MustParsePrefix("224.0.0.0/3"),
}

func hostname(value string) string {
	host := strings.ToLower(strings.TrimSuffix(value, "."))
	labels := strings.Split(host, ".")
	if len(host) > 253 || len(labels) < 2 || !strings.ContainsAny(labels[len(labels)-1], "abcdefghijklmnopqrstuvwxyz") {
		return ""
	}
	for _, label := range labels {
		if !dnsLabel.MatchString(label) {
			return ""
		}
	}
	return host
}

func allowlist(value string) (map[string]struct{}, error) {
	if strings.TrimSpace(value) == "" {
		value = "openrouter.ai"
	}
	hosts := make(map[string]struct{})
	for _, entry := range strings.Split(value, ",") {
		host := hostname(strings.TrimSpace(entry))
		if host == "" {
			return nil, errors.New("invalid PHOTOCULL_EGRESS_ALLOW: expected comma-separated DNS hostnames")
		}
		hosts[host] = struct{}{}
	}
	return hosts, nil
}

func publicIPv4(ip netip.Addr) bool {
	if !ip.Is4() {
		return false
	}
	for _, prefix := range nonPublicIPv4 {
		if prefix.Contains(ip) {
			return false
		}
	}
	return true
}

type proxy struct {
	allowed map[string]struct{}
	resolve func(context.Context, string, string) ([]netip.Addr, error)
	dial    func(context.Context, string, string) (net.Conn, error)
	slots   chan struct{}
}

func deny(w http.ResponseWriter, status int, reason string) {
	log.Printf("decision=deny reason=%s", reason)
	w.Header().Set("Connection", "close")
	http.Error(w, http.StatusText(status), status)
}

func (p proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodConnect {
		deny(w, http.StatusMethodNotAllowed, "method")
		return
	}
	host, port, err := net.SplitHostPort(r.RequestURI)
	if err != nil || port != "443" || r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
		deny(w, http.StatusForbidden, "authority")
		return
	}
	host = hostname(host)
	if _, ok := p.allowed[host]; !ok {
		deny(w, http.StatusForbidden, "host")
		return
	}
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	default:
		deny(w, http.StatusServiceUnavailable, "capacity")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), upstreamTimeout)
	defer cancel()
	ips, err := p.resolve(ctx, "ip4", host+".")
	if err != nil || len(ips) == 0 {
		deny(w, http.StatusBadGateway, "dns")
		return
	}
	for _, ip := range ips {
		if !publicIPv4(ip) {
			deny(w, http.StatusForbidden, "non_public_address")
			return
		}
	}

	var upstream net.Conn
	for _, ip := range ips {
		// Dial the checked address directly so DNS cannot change between check and use.
		upstream, err = p.dial(ctx, "tcp4", net.JoinHostPort(ip.String(), port))
		if err == nil {
			break
		}
	}
	if err != nil {
		deny(w, http.StatusBadGateway, "upstream")
		return
	}
	defer upstream.Close()

	client, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		deny(w, http.StatusInternalServerError, "tunnel")
		return
	}
	defer client.Close()
	deadline := time.Now().Add(tunnelTimeout)
	_ = client.SetDeadline(deadline)
	_ = upstream.SetDeadline(deadline)
	if _, err = buffered.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		log.Print("decision=deny reason=tunnel")
		return
	}
	if err = buffered.Flush(); err != nil {
		log.Print("decision=deny reason=tunnel")
		return
	}
	log.Printf("decision=allow host=%s port=443", host)
	done := make(chan struct{})
	go func() {
		_, _ = io.Copy(upstream, buffered)
		// Propagate EOF while letting the opposite direction finish.
		if tcp, ok := upstream.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		close(done)
	}()
	_, _ = io.Copy(client, upstream)
	_ = client.Close()
	_ = upstream.Close()
	<-done
}

// Only the optional, dedicated settings mount is visible to the proxy.
func configuredAllowlist(env, path string) (map[string]struct{}, error) {
	if strings.TrimSpace(env) != "" || path == "" {
		return allowlist(env)
	}
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return allowlist("")
	}
	if err != nil {
		return nil, errors.New("cannot read Photocull managed settings")
	}
	defer file.Close()
	const limit = 256 << 10
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || len(data) > limit {
		return nil, errors.New("cannot read Photocull managed settings")
	}
	var settings struct {
		Overrides map[string]json.RawMessage `json:"overrides"`
	}
	if json.Unmarshal(data, &settings) != nil || settings.Overrides == nil {
		return nil, errors.New("invalid Photocull managed settings")
	}
	raw, ok := settings.Overrides["network.egress_allow"]
	if !ok {
		return allowlist("")
	}
	var names []string
	if json.Unmarshal(raw, &names) != nil || names == nil || len(names) > 128 {
		return nil, errors.New("invalid managed egress allowlist")
	}
	hosts := make(map[string]struct{})
	for _, name := range names {
		host := hostname(strings.TrimSpace(name))
		if host == "" {
			return nil, errors.New("invalid managed egress allowlist")
		}
		hosts[host] = struct{}{}
	}
	return hosts, nil
}

func main() {
	hosts, err := configuredAllowlist(os.Getenv("PHOTOCULL_EGRESS_ALLOW"), os.Getenv("PHOTOCULL_EGRESS_SETTINGS"))
	if err != nil {
		log.Fatal(err)
	}
	handler := proxy{
		allowed: hosts,
		resolve: net.DefaultResolver.LookupNetIP,
		dial:    (&net.Dialer{}).DialContext,
		slots:   make(chan struct{}, 32),
	}
	server := &http.Server{
		Addr:                         ":3128",
		Handler:                      handler,
		DisableGeneralOptionsHandler: true,
		ReadHeaderTimeout:            5 * time.Second,
		WriteTimeout:                 15 * time.Second,
		MaxHeaderBytes:               8 << 10,
	}
	log.Fatal(server.ListenAndServe())
}
