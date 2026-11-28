FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /egress-proxy .

FROM scratch
COPY --from=build /egress-proxy /egress-proxy
USER 65534:65534
ENTRYPOINT ["/egress-proxy"]
