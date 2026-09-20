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

// identity is everything the server needs to serve one paired account's
// requests independently -- its own Handler (so its own policyLayers/
// homeRoot dispatch, isolated from every other identity even though
// homeRoot is typically the same underlying directory for all of them),
// and its own sign-out callbacks. One daemon process can hold several of
// these at once -- the same physical machine paired to more than one
// Casper account simultaneously (see cmd/casper/daemon.go's
// pairedIdentity, the one-level-up counterpart that also tracks
// username/deviceToken for presence reporting, which this package has no
// need to know about).
type identity struct {
	handler      *commands.Handler
	clearSession func() // synchronous -- run before the /api/shutdown response is sent
	onSignOut    func() // async -- run after, once the response is already on the wire
}

// Server holds everything the HTTP handlers need. Each identity's
// onSignOut is called (in its own goroutine, by the /api/shutdown handler)
// after the response has already been written -- unlike the one-shot agent
// this used to be, signing out no longer ends the process, and (since
// multi-account pairing) no longer disturbs any OTHER identity still
// paired here either: onSignOut is expected to clear just that one
// identity's local state (its own share of session.json, relay presence),
// leaving every other paired identity, and the daemon itself, running. The
// process only ever exits via the status-bar "Quit" item (see
// cmd/casper/main.go), which calls Shutdown() directly.
type Server struct {
	Logger *log.Logger

	mu         sync.RWMutex
	identities map[string]*identity // keyed by command_key
	httpServer *http.Server
}

func New(logger *log.Logger) *Server {
	return &Server{Logger: logger, identities: make(map[string]*identity)}
}

// AddIdentity registers (or, for a command_key already in use -- re-pairing
// the same account -- replaces) one paired identity, live: needed because
// pairing a fresh account into an already-running daemon must take effect
// without restarting the HTTP listener.
func (s *Server) AddIdentity(commandKey string, h *commands.Handler, clearSession, onSignOut func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.identities[commandKey] = &identity{handler: h, clearSession: clearSession, onSignOut: onSignOut}
}

// RemoveIdentity is a no-op if commandKey isn't currently registered
// (e.g. called twice, or for an identity that was never added).
func (s *Server) RemoveIdentity(commandKey string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.identities, commandKey)
}

// HasAPIKey reports whether ANY identity is currently paired -- used by the
// daemon's status-bar state machine to tell "signed in (to at least one
// account)" from "signed out entirely" without exposing any key itself
// outside this package.
func (s *Server) HasAPIKey() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.identities) > 0
}

func (s *Server) resolveIdentity(r *http.Request) (*identity, bool) {
	key := r.Header.Get("X-API-Key")
	if key == "" {
		s.Logger.Printf("REJECTED request with invalid/missing API key")
		return nil, false
	}
	s.mu.RLock()
	id, ok := s.identities[key]
	s.mu.RUnlock()
	if !ok {
		s.Logger.Printf("REJECTED request with invalid/missing API key")
	}
	return id, ok
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.resolveIdentity(r); !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// Deliberately not reachable from the assistant's command schema (unlike
// /api/command's ACTION_HANDLERS) -- only the web app's dedicated sign-out
// button calls this, using that account's own command_key, which is what
// resolves to exactly the one identity this signs out. Unlike the one-shot
// agent this used to be, handling this request no longer ends the process
// -- it just clears that one identity's local state (this key included, via
// onSignOut) and returns it to an idle, unpaired state, leaving every other
// identity and the daemon itself untouched; it stays running, waiting for a
// fresh casper:// pairing.
func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	id, ok := s.resolveIdentity(r)
	if !ok {
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
	if id.clearSession != nil {
		id.clearSession()
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "message": "Signed out."})
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	if id.onSignOut != nil {
		go id.onSignOut()
	}
}

func (s *Server) handleCommand(w http.ResponseWriter, r *http.Request) {
	id, ok := s.resolveIdentity(r)
	if !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	var req commands.Request
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "Invalid request body."})
		return
	}
	s.Logger.Printf("RUN action=%s path=%q", req.Action, req.Path)
	result, err := id.handler.Dispatch(&req)
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
