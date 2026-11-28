package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func TestAllowlist(t *testing.T) {
	for _, value := range []string{"", "OPENROUTER.AI., api.example.com"} {
		hosts, err := allowlist(value)
		if _, ok := hosts["openrouter.ai"]; err != nil || !ok {
			t.Fatalf("expected default host in %v, err=%v", hosts, err)
		}
		if value != "" && len(hosts) != 2 {
			t.Fatalf("expected both configured hosts in %v", hosts)
		}
	}
	for _, value := range []string{
		"*.example.com", "openrouter.ai:443", "localhost", "192.168.1.1",
		"[::1]", "https://example.com", "openrouter.ai,",
	} {
		if _, err := allowlist(value); err == nil {
			t.Errorf("accepted invalid entry %q", value)
		}
	}
}

func TestConnectPolicy(t *testing.T) {
	defaultHosts, _ := allowlist("")
	for _, tc := range []struct {
		name, method, authority string
		ip                      string
		want                    int
	}{
		{"unlisted host", "CONNECT", "example.com:443", "", 403},
		{"subdomain", "CONNECT", "www.openrouter.ai:443", "", 403},
		{"host lookalike", "CONNECT", "openrouter.ai.example.com:443", "", 403},
		{"LAN literal", "CONNECT", "192.168.1.1:443", "", 403},
		{"IPv6 literal", "CONNECT", "[::1]:443", "", 403},
		{"other port", "CONNECT", "openrouter.ai:80", "", 403},
		{"plaintext", "GET", "http://openrouter.ai/", "", 405},
		{"post", "POST", "http://openrouter.ai/", "", 405},
		{"global options", "OPTIONS", "*", "", 405},
		{"loopback DNS", "CONNECT", "openrouter.ai:443", "127.0.0.1", 403},
		{"LAN DNS", "CONNECT", "openrouter.ai:443", "192.168.1.1", 403},
		{"Docker DNS", "CONNECT", "openrouter.ai:443", "172.18.0.2", 403},
		{"metadata DNS", "CONNECT", "openrouter.ai:443", "169.254.169.254", 403},
		{"DNS failure", "CONNECT", "openrouter.ai:443", "invalid", 502},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := proxy{
				allowed: defaultHosts,
				slots:   make(chan struct{}, 1),
				resolve: func(context.Context, string, string) ([]netip.Addr, error) {
					if tc.ip == "" {
						t.Fatal("unauthorized authority reached DNS")
					}
					if tc.ip == "invalid" {
						return nil, errors.New("fixture DNS failure")
					}
					// A public address paired with a private answer must still be rejected.
					return []netip.Addr{netip.MustParseAddr("1.1.1.1"), netip.MustParseAddr(tc.ip)}, nil
				},
				dial: func(context.Context, string, string) (net.Conn, error) {
					t.Fatal("denied request reached upstream dial")
					return nil, nil
				},
			}
			request := httptest.NewRequest(tc.method, tc.authority, nil)
			reply := httptest.NewRecorder()
			p.ServeHTTP(reply, request)
			if reply.Code != tc.want {
				t.Fatalf("got %d, want %d", reply.Code, tc.want)
			}
		})
	}
}

func TestConnectTunnelAndLogs(t *testing.T) {
	var logs bytes.Buffer
	writer := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(writer) })

	hosts, _ := allowlist("api.example.com")
	upstream, peer := net.Pipe()
	defer peer.Close()
	_ = peer.SetDeadline(time.Now().Add(2 * time.Second))
	resolved := make(chan string, 1)
	dialed := make(chan string, 1)
	p := proxy{
		allowed: hosts,
		slots:   make(chan struct{}, 1),
		resolve: func(_ context.Context, network, name string) ([]netip.Addr, error) {
			resolved <- network + " " + name
			return []netip.Addr{netip.MustParseAddr("1.1.1.1")}, nil
		},
		dial: func(_ context.Context, network, address string) (net.Conn, error) {
			dialed <- network + " " + address
			return upstream, nil
		},
	}
	rejected := httptest.NewRequest("GET", "http://example.com/fixture-query", nil)
	rejected.Header.Set("Authorization", "fixture-auth")
	p.ServeHTTP(httptest.NewRecorder(), rejected)
	server := httptest.NewServer(p)
	defer server.Close()
	client, err := net.DialTimeout("tcp", server.Listener.Addr().String(), 2*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	// Include bytes immediately after the headers to exercise the buffered reader.
	_, err = fmt.Fprint(client, "CONNECT API.EXAMPLE.COM.:443 HTTP/1.1\r\nHost: API.EXAMPLE.COM.:443\r\nAuthorization: fixture-auth\r\n\r\nhello")
	if err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(client)
	resp, err := http.ReadResponse(reader, &http.Request{Method: http.MethodConnect})
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("CONNECT failed, response=%v, err=%v", resp, err)
	}
	if got := <-resolved; got != "ip4 api.example.com." {
		t.Fatalf("unexpected resolution: %q", got)
	}
	if got := <-dialed; got != "tcp4 1.1.1.1:443" {
		t.Fatalf("unexpected dial: %q", got)
	}
	buf := make([]byte, 5)
	if _, err := io.ReadFull(peer, buf); err != nil || string(buf) != "hello" {
		t.Fatalf("client bytes did not reach upstream: %q %v", buf, err)
	}
	if _, err := peer.Write([]byte("world")); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(reader, buf); err != nil || string(buf) != "world" {
		t.Fatalf("upstream bytes did not reach client: %q %v", buf, err)
	}

	output := logs.String()
	if !strings.Contains(output, "decision=deny reason=method") ||
		!strings.Contains(output, "decision=allow host=api.example.com port=443") ||
		strings.Contains(output, "fixture-") {
		t.Fatalf("unexpected decision log: %q", output)
	}
}
