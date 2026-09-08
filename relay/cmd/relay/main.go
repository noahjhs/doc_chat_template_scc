// Command relay is the self-hosted WebSocket relay: agents dial in on
// /connect and register under a random routing key; the deployed web app
// reaches them via /agent/{key}/{rest...}, which proxies over that live
// connection. Replaces the per-user ephemeral cloudflared quick-tunnel the
// Go agent used to launch.
package main

import (
	"fmt"
	"log"
	"net/http"
	"os"

	"casper-relay/internal/httpproxy"
	"casper-relay/internal/registry"
	"casper-relay/internal/wire"
	"casper-relay/internal/wsconnect"
)

func main() {
	port := os.Getenv("RELAY_PORT")
	if port == "" {
		port = "8600"
	}
	logger := log.New(os.Stdout, "", log.LstdFlags)
	reg := registry.New()

	mux := http.NewServeMux()
	mux.HandleFunc("/connect", wsconnect.Handler(reg, logger))
	mux.HandleFunc("/agent/{key}/{rest...}", httpproxy.Handler(reg, logger, wire.TimeoutFor))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	addr := fmt.Sprintf("0.0.0.0:%s", port)
	logger.Printf("casper-relay listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		logger.Fatalf("relay server error: %s", err)
	}
}
