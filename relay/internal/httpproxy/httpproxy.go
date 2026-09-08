// Package httpproxy implements the "/agent/{key}/{rest...}" endpoint: turns
// an ordinary HTTP request into a wire.Frame, sends it to the matching
// agent's live connection, and translates the response (or failure) back
// into a normal HTTP response.
package httpproxy

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"casper-relay/internal/registry"
	"casper-relay/internal/wire"
)

// Handler builds the "/agent/{key}/{rest...}" proxy handler. timeoutFor is
// injectable (production wiring passes wire.TimeoutFor) so tests can use a
// much shorter deadline than the real 5s/15s values without actually
// waiting on a wall-clock timer.
func Handler(reg *registry.Registry, logger *log.Logger, timeoutFor func(string) time.Duration) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		key := r.PathValue("key")
		rest := r.PathValue("rest")
		path := "/" + strings.TrimPrefix(rest, "/")

		conn, ok := reg.Get(key)
		if !ok {
			logger.Printf("PROXY %s %s key=%s -> 503 (not connected)", r.Method, path, key)
			writeError(w, http.StatusServiceUnavailable, "agent not connected")
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			writeError(w, http.StatusBadRequest, "couldn't read request body")
			return
		}

		headers := map[string]string{}
		if ct := r.Header.Get("Content-Type"); ct != "" {
			headers["Content-Type"] = ct
		}
		if apiKey := r.Header.Get("X-Api-Key"); apiKey != "" {
			headers["X-Api-Key"] = apiKey
		}

		frame := wire.Frame{
			Method:  r.Method,
			Path:    path,
			Headers: headers,
			Body:    base64.StdEncoding.EncodeToString(body),
		}

		ctx, cancel := context.WithTimeout(r.Context(), timeoutFor(path))
		defer cancel()

		resp, err := conn.Do(ctx, frame)
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) {
				logger.Printf("PROXY %s %s key=%s -> 504 (timeout)", r.Method, path, key)
				writeError(w, http.StatusGatewayTimeout, "agent did not respond in time")
			} else {
				logger.Printf("PROXY %s %s key=%s -> 502 (%s)", r.Method, path, key, err)
				writeError(w, http.StatusBadGateway, "agent disconnected")
			}
			return
		}
		if resp.Error != "" {
			logger.Printf("PROXY %s %s key=%s -> 502 (agent error: %s)", r.Method, path, key, resp.Error)
			writeError(w, http.StatusBadGateway, resp.Error)
			return
		}

		respBody, err := base64.StdEncoding.DecodeString(resp.Body)
		if err != nil {
			writeError(w, http.StatusBadGateway, "malformed response from agent")
			return
		}
		for k, v := range resp.Headers {
			w.Header().Set(k, v)
		}
		status := resp.Status
		if status == 0 {
			status = http.StatusOK
		}
		logger.Printf("PROXY %s %s key=%s -> %d", r.Method, path, key, status)
		w.WriteHeader(status)
		_, _ = w.Write(respBody)
	}
}

func writeError(w http.ResponseWriter, status int, detail string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": detail})
}
