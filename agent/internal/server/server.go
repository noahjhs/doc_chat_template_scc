// Package server implements the main local API: /api/health, /api/shutdown,
// /api/command -- a direct port of casper_tool.py's FastAPI app.
package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"sync"

	"casper-agent/internal/commands"
)

// Server holds everything the HTTP handlers need. OnSignOut is called (in
// its own goroutine, by the /api/shutdown handler) after the response has
// already been written -- unlike the one-shot agent this used to be, signing
// out no longer ends the process: OnSignOut is expected to clear local state
// (session file, relay tunnel, presence) and leave the daemon running, idle,
// waiting to be paired again. The process itself only ever exits via the
// status-bar "Quit" item (see cmd/casper/main.go), which calls Shutdown()
// directly.
type Server struct {
	Commands     *commands.Handler
	Logger       *log.Logger
	OnSignOut    func()
	ClearSession func()
	// RoutingKey and AllowedOrigin back /api/whoami (see handleWhoami) --
	// fixed for the process's whole lifetime, set once by cmd/casper/main.go
	// right after construction, so (unlike apiKey) they need no mutex.
	RoutingKey    string
	AllowedOrigin string

	mu         sync.RWMutex
	apiKey     string
	httpServer *http.Server
}

func New(apiKey string, cmdHandler *commands.Handler, logger *log.Logger) *Server {
	s := &Server{Commands: cmdHandler, Logger: logger}
	s.SetAPIKey(apiKey)
	return s
}

// SetAPIKey changes the key this server accepts, live -- needed because
// re-pairing an already-running daemon (a fresh token from a new browser
// login) must take effect without restarting the HTTP listener. Passing ""
// rejects every request (matches the zero-value behavior requireAPIKey
// always had for an unset key).
func (s *Server) SetAPIKey(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.apiKey = key
}

func (s *Server) getAPIKey() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.apiKey
}

// HasAPIKey reports whether the server currently has a key set at all --
// used by the daemon's status-bar state machine to tell "signed in" from
// "signed out" without exposing the key itself outside this package.
func (s *Server) HasAPIKey() bool {
	return s.getAPIKey() != ""
}

func (s *Server) requireAPIKey(r *http.Request) bool {
	key := r.Header.Get("X-API-Key")
	expected := s.getAPIKey()
	if expected == "" || key != expected {
		s.Logger.Printf("REJECTED request with invalid/missing API key")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// handleWhoami is the one deliberately unauthenticated endpoint -- it lets
// a browser tab ask whether a Casper daemon is running on the SAME
// physical machine it's loaded on (see pages/chat.py's local-host
// detection, used to default an unspecified "which machine" tool
// argument to wherever the chat session itself is happening), which by
// definition has to work without a credential the browser doesn't have
// yet. routing_key isn't a secret -- see routingkey.go's own doc comment:
// it's just a relay-addressing path segment, already visible in every
// connected host's local_agent_url anyway -- so returning it here doesn't
// expose anything a signed-in user's own browser couldn't already see.
// CORS is restricted to this deployment's own configured app origin
// (never "*"), set via AllowedOrigin, so an unrelated website's JS can't
// use this to fingerprint which local port happens to have a Casper
// daemon on it.
func (s *Server) handleWhoami(w http.ResponseWriter, r *http.Request) {
	if s.AllowedOrigin != "" {
		w.Header().Set("Access-Control-Allow-Origin", s.AllowedOrigin)
	}
	writeJSON(w, http.StatusOK, map[string]string{"routing_key": s.RoutingKey})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKey(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// Deliberately not reachable from the assistant's command schema (unlike
// /api/command's ACTION_HANDLERS) -- only the web app's dedicated sign-out
// button calls this. Unlike the one-shot agent this used to be, handling
// this request no longer ends the process -- it just clears local session
// state (this API key included, via OnSignOut) and returns the daemon to an
// idle, unpaired state; it stays running, waiting for a fresh casper://
// pairing.
func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKey(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	s.Logger.Printf("Sign-out requested -- clearing session, staying up.")
	// Cleared right away, not from the background goroutine below -- this
	// token is also being revoked server-side right now (the web app's
	// sign-out flow calls the auth service separately), so there's nothing
	// left this file could still be useful for once this request is
	// handled, and leaving it in place only makes the *next* launch
	// discover it's dead the slow way.
	if s.ClearSession != nil {
		s.ClearSession()
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "message": "Signed out."})
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	if s.OnSignOut != nil {
		go s.OnSignOut()
	}
}

func (s *Server) handleCommand(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKey(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	var req commands.Request
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid request body."})
		return
	}
	req.ApplyDefaults()

	s.Logger.Printf("RUN action=%s path=%q destination=%q pattern=%q", req.Action, req.Path, req.Destination, req.Pattern)
	result, err := s.Commands.Dispatch(&req)
	if err != nil {
		if ae, ok := err.(*commands.ActionError); ok {
			// Mirrors the Python version's HTTPException(400, detail=...) --
			// a client-facing "the request itself is invalid" error, not a
			// command that ran and failed.
			s.Logger.Printf("REJECTED action=%s: %s", req.Action, ae.Detail)
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": ae.Detail})
			return
		}
		// Mirrors the Python version's `except OSError as e: return
		// _fail(str(e))` -- an OS-level failure during a well-formed
		// request still comes back as 200 success=false, not an HTTP error.
		s.Logger.Printf("FAIL action=%s: %s", req.Action, err)
		writeJSON(w, http.StatusOK, commands.Result{Success: false, Cwd: "", Stdout: "", Stderr: err.Error()})
		return
	}
	if result.Success {
		s.Logger.Printf("OK   action=%s", req.Action)
	} else {
		s.Logger.Printf("FAIL action=%s", req.Action)
	}
	writeJSON(w, http.StatusOK, result)
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", s.handleHealth)
	mux.HandleFunc("/api/whoami", s.handleWhoami)
	mux.HandleFunc("/api/shutdown", s.handleShutdown)
	mux.HandleFunc("/api/command", s.handleCommand)
	return mux
}

// ListenAndServe blocks until the server is asked to Shutdown (or fails to
// start). Built explicitly (rather than a bare http.ListenAndServe) so
// Shutdown() has a concrete *http.Server to call -- Go's net/http.Server has
// a real graceful-shutdown mechanism built in, unlike uvicorn's should_exit
// flag, which this replaces.
func (s *Server) ListenAndServe(addr string) error {
	s.httpServer = &http.Server{Addr: addr, Handler: s.Handler()}
	err := s.httpServer.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func (s *Server) Shutdown() {
	if s.httpServer != nil {
		_ = s.httpServer.Shutdown(context.Background())
	}
}
