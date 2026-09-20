package server

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"casper-agent/internal/commands"
)

func newTestServer() *Server {
	return New(log.New(logDiscard{}, "", 0))
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

func doCommand(t *testing.T, ts *httptest.Server, key string, req commands.Request) commands.Result {
	t.Helper()
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	httpReq, err := http.NewRequest(http.MethodPost, ts.URL+"/api/command", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	httpReq.Header.Set("X-API-Key", key)
	resp, err := http.DefaultClient.Do(httpReq)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var result commands.Result
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	return result
}

func TestUnknownAPIKeyIsRejected(t *testing.T) {
	s := newTestServer()
	s.AddIdentity("key-a", commands.New(""), nil, nil)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	if got := doHealth(t, ts, "key-a"); got != http.StatusOK {
		t.Fatalf("expected 200 for a registered key, got %d", got)
	}
	if got := doHealth(t, ts, "bogus"); got != http.StatusUnauthorized {
		t.Fatalf("expected 401 for an unregistered key, got %d", got)
	}
	if got := doHealth(t, ts, ""); got != http.StatusUnauthorized {
		t.Fatalf("expected 401 for a missing key, got %d", got)
	}
}

// TestMultipleIdentities_EachDispatchesToItsOwnHandler is the core
// multi-account-pairing guarantee: two identities registered on the same
// server, each with its own commands.Handler (its own cached policy
// layers), must never see the other's state -- confirmed here via
// list_policy_layers, whose response reflects exactly the calling
// identity's own Handler.
func TestMultipleIdentities_EachDispatchesToItsOwnHandler(t *testing.T) {
	s := newTestServer()

	handlerA := commands.New("")
	handlerA.SetPolicyLayers([]commands.PolicyLayer{{ID: 1, Name: "A-only-layer"}})
	handlerB := commands.New("") // no policy layers at all

	s.AddIdentity("key-a", handlerA, nil, nil)
	s.AddIdentity("key-b", handlerB, nil, nil)

	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	resultA := doCommand(t, ts, "key-a", commands.Request{Action: "list_policy_layers"})
	if !strings.Contains(resultA.Stdout, "A-only-layer") {
		t.Fatalf("expected key-a's response to reflect its own handler's policy layers, got %q", resultA.Stdout)
	}

	resultB := doCommand(t, ts, "key-b", commands.Request{Action: "list_policy_layers"})
	if strings.Contains(resultB.Stdout, "A-only-layer") {
		t.Fatalf("key-b's response leaked key-a's policy layer: %q", resultB.Stdout)
	}
}

func TestRemoveIdentity_OnlyThatKeyStopsWorking(t *testing.T) {
	s := newTestServer()
	s.AddIdentity("key-a", commands.New(""), nil, nil)
	s.AddIdentity("key-b", commands.New(""), nil, nil)
	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	s.RemoveIdentity("key-a")

	if got := doHealth(t, ts, "key-a"); got != http.StatusUnauthorized {
		t.Fatalf("expected 401 for a removed key, got %d", got)
	}
	if got := doHealth(t, ts, "key-b"); got != http.StatusOK {
		t.Fatalf("expected the other identity to be unaffected, got %d", got)
	}
}

func TestHasAPIKey_ReflectsIdentityCount(t *testing.T) {
	s := newTestServer()
	if s.HasAPIKey() {
		t.Fatal("expected HasAPIKey to report false with zero identities")
	}
	s.AddIdentity("key-a", commands.New(""), nil, nil)
	if !s.HasAPIKey() {
		t.Fatal("expected HasAPIKey to report true once an identity is added")
	}
	s.RemoveIdentity("key-a")
	if s.HasAPIKey() {
		t.Fatal("expected HasAPIKey to report false once the last identity is removed")
	}
}

// TestHandleShutdown_OnlySignsOutTheResolvedIdentity is the regression test
// for the daemon-conversion behavior change (signing out must no longer
// end the process) AND for multi-account isolation (signing out one
// account must never disturb another paired to the same daemon). Confirms
// clearSession runs synchronously (before the response is sent), onSignOut
// runs afterward, only the resolved identity's callbacks fire, and the
// HTTP server keeps serving the OTHER identity's requests throughout.
func TestHandleShutdown_OnlySignsOutTheResolvedIdentity(t *testing.T) {
	s := newTestServer()

	var mu sync.Mutex
	var aClearedBeforeResponse, aSignedOut, bClearedOrSignedOut bool
	signedOutA := make(chan struct{})

	s.AddIdentity("key-a", commands.New(""), func() {
		mu.Lock()
		aClearedBeforeResponse = true
		mu.Unlock()
	}, func() {
		mu.Lock()
		aSignedOut = true
		mu.Unlock()
		close(signedOutA)
	})
	s.AddIdentity("key-b", commands.New(""), func() {
		mu.Lock()
		bClearedOrSignedOut = true
		mu.Unlock()
	}, func() {
		mu.Lock()
		bClearedOrSignedOut = true
		mu.Unlock()
	})

	ts := httptest.NewServer(s.Handler())
	defer ts.Close()

	req, _ := http.NewRequest(http.MethodPost, ts.URL+"/api/shutdown", nil)
	req.Header.Set("X-API-Key", "key-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

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
	if !aClearedBeforeResponse {
		t.Error("expected key-a's clearSession to have run by the time the response was received")
	}
	mu.Unlock()

	select {
	case <-signedOutA:
	case <-time.After(2 * time.Second):
		t.Fatal("key-a's onSignOut was never called")
	}
	mu.Lock()
	if !aSignedOut {
		t.Error("expected key-a's onSignOut to have been called")
	}
	if bClearedOrSignedOut {
		t.Error("expected key-b's callbacks to never fire from key-a's sign-out")
	}
	mu.Unlock()

	// key-a is gone; key-b, never touched, still works. Neither ended the
	// process (the old one-shot-agent behavior this replaced).
	if got := doHealth(t, ts, "key-b"); got != http.StatusOK {
		t.Fatalf("expected key-b to still be accepted after key-a signed out, got %d", got)
	}

	// A fresh identity (a re-pairing) still registers live, same as before.
	s.AddIdentity("key-c", commands.New(""), nil, nil)
	if got := doHealth(t, ts, "key-c"); got != http.StatusOK {
		t.Fatalf("expected the server to still accept new identities after a sign-out, got %d", got)
	}
}
