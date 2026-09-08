// Package server implements the main local API: /api/health, /api/shutdown,
// /api/command -- a direct port of casper_tool.py's FastAPI app.
package server

import (
	"context"
	"encoding/json"
	"log"
	"net/http"

	"casper-agent/internal/commands"
)

// Server holds everything the HTTP handlers need. OnShutdownRequested is
// called (in its own goroutine, by the /api/shutdown handler) after the
// response has already been written -- it's expected to show the farewell
// dialog and then call Shutdown() itself, mirroring casper_tool.py's
// _farewell_then_exit()/should_exit split (the response must reach the
// caller before the process actually starts winding down).
type Server struct {
	APIKey              string
	Commands            *commands.Handler
	Logger              *log.Logger
	OnShutdownRequested func()
	ClearSession        func()

	httpServer *http.Server
}

func New(apiKey string, cmdHandler *commands.Handler, logger *log.Logger) *Server {
	return &Server{APIKey: apiKey, Commands: cmdHandler, Logger: logger}
}

func (s *Server) requireAPIKey(r *http.Request) bool {
	key := r.Header.Get("X-API-Key")
	if s.APIKey == "" || key != s.APIKey {
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

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKey(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// Deliberately not reachable from the assistant's command schema (unlike
// /api/command's ACTION_HANDLERS) -- only the web app's dedicated sign-out
// button calls this, same as the Python version's comment on
// _farewell_then_exit() explains.
func (s *Server) handleShutdown(w http.ResponseWriter, r *http.Request) {
	if !s.requireAPIKey(r) {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"detail": "Invalid or missing X-API-Key."})
		return
	}
	s.Logger.Printf("Shutdown requested -- showing the farewell dialog, then exiting.")
	// Cleared right away, not from the background goroutine below -- this
	// token is also being revoked server-side right now (the web app's
	// sign-out flow calls the auth service separately), so there's nothing
	// left this file could still be useful for once this request is
	// handled, and leaving it in place only makes the *next* launch
	// discover it's dead the slow way.
	if s.ClearSession != nil {
		s.ClearSession()
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true, "message": "Shutting down."})
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	if s.OnShutdownRequested != nil {
		go s.OnShutdownRequested()
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
