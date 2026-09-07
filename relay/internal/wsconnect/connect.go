// Package wsconnect implements the "/connect" endpoint agents dial into.
package wsconnect

import (
	"log"
	"net/http"

	"github.com/coder/websocket"

	"casper-relay/internal/registry"
)

func Handler(reg *registry.Registry, logger *log.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		key := r.URL.Query().Get("key")
		if key == "" {
			http.Error(w, "missing key", http.StatusBadRequest)
			return
		}

		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			logger.Printf("connect: accept failed for key=%s: %s", key, err)
			return
		}

		conn := reg.NewConn(key, c)
		reg.Register(key, conn)
		logger.Printf("agent connected: key=%s", key)

		conn.Serve(r.Context())

		reg.Unregister(key, conn)
		logger.Printf("agent disconnected: key=%s", key)
	}
}
