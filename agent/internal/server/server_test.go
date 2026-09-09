package server

import (
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"casper-agent/internal/commands"
)

func newTestServer() *Server {
	return New("initial-key", commands.New("/tmp"), log.New(logDiscard{}, "", 0))
}

type logDiscard struct{}

func (logDiscard) Write(p []byte) (int, error) { return len(p), nil }

func doHealth(t *testing.T, ts *httptest.Server, key string) int {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, ts.URL+"/api/health", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("X-API-Key", key)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

func TestSetAPIKey_TakesEffectLive(t *testing.T) {
	s := newTestServer()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	if got := doHealth(t, ts, "initial-key"); got != http.StatusOK {
		t.Fatalf("expected 200 with the initial key, got %d", got)
	}

	s.SetAPIKey("rotated-key")

	if got := doHealth(t, ts, "initial-key"); got != http.StatusUnauthorized {
		t.Fatalf("expected 401 with the old key after rotation, got %d", got)
	}
	if got := doHealth(t, ts, "rotated-key"); got != http.StatusOK {
		t.Fatalf("expected 200 with the new key after rotation, got %d", got)
	}
}

func TestSetAPIKey_EmptyRejectsEverything(t *testing.T) {
	s := newTestServer()
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	s.SetAPIKey("")

	if got := doHealth(t, ts, ""); got != http.StatusUnauthorized {
		t.Fatalf("expected 401 once the key is cleared, got %d", got)
	}
	if s.HasAPIKey() {
		t.Fatal("expected HasAPIKey to report false once cleared")
	}
}

// TestHandleShutdown_ClearsSessionAndSignalsWithoutStoppingServer is the
// regression test for the daemon-conversion behavior change: signing out
// must no longer end the process. Confirms ClearSession runs synchronously
// (before the response is sent), OnSignOut runs afterward, and the HTTP
// server keeps serving requests once it's done -- unlike the old
// OnShutdownRequested, which called Shutdown() and ended the process.
func TestHandleShutdown_ClearsSessionAndSignalsWithoutStoppingServer(t *testing.T) {
	s := newTestServer()

	var mu sync.Mutex
	var clearedBeforeResponse, signOutCalled bool
	responded := make(chan struct{})
	signedOut := make(chan struct{})

	s.ClearSession = func() {
		mu.Lock()
		clearedBeforeResponse = true
		mu.Unlock()
	}
	s.OnSignOut = func() {
		mu.Lock()
		signOutCalled = true
		mu.Unlock()
		close(signedOut)
	}

	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/shutdown", nil)
	req.Header.Set("X-API-Key", "initial-key")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	close(responded)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 from /api/shutdown, got %d", resp.StatusCode)
	}
	var body struct {
		Success bool `json:"success"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if !body.Success {
		t.Fatal("expected success:true in the /api/shutdown response")
	}

	mu.Lock()
	if !clearedBeforeResponse {
		t.Error("expected ClearSession to have run by the time the response was received")
	}
	mu.Unlock()

	select {
	case <-signedOut:
	case <-time.After(2 * time.Second):
		t.Fatal("OnSignOut was never called")
	}
	mu.Lock()
	if !signOutCalled {
		t.Error("expected OnSignOut to have been called")
	}
	mu.Unlock()

	// The defining behavior change: the server is still up afterward (the
	// old code's OnShutdownRequested called Shutdown() here and ended the
	// process). A fresh key set by a re-pairing should still work.
	s.SetAPIKey("fresh-key")
	if got := doHealth(t, ts, "fresh-key"); got != http.StatusOK {
		t.Fatalf("expected the server to still be serving requests after sign-out, got %d", got)
	}
}
