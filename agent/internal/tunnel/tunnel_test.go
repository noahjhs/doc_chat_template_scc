package tunnel

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestDoLoopback_RoundTrip(t *testing.T) {
	agentServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Api-Key") != "test-key" {
			t.Errorf("expected X-Api-Key header to be forwarded, got %q", r.Header.Get("X-Api-Key"))
		}
		body := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true,"cwd":"/x","stdout":"/x","stderr":""}`))
	}))
	defer agentServer.Close()

	port, err := strconv.Atoi(strings.Split(agentServer.URL, ":")[2])
	if err != nil {
		t.Fatal(err)
	}

	frame := Frame{
		ID:      "1",
		Method:  "POST",
		Path:    "/api/command",
		Headers: map[string]string{"X-Api-Key": "test-key", "Content-Type": "application/json"},
		Body:    base64.StdEncoding.EncodeToString([]byte(`{"action":"pwd"}`)),
	}

	resp := doLoopback(&http.Client{}, port, frame)
	if resp.Status != http.StatusOK {
		t.Fatalf("expected 200, got %d (error=%q)", resp.Status, resp.Error)
	}
	decoded, err := base64.StdEncoding.DecodeString(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(decoded), `"cwd":"/x"`) {
		t.Fatalf("unexpected response body: %s", decoded)
	}
}

func TestDoLoopback_ConnectionRefused(t *testing.T) {
	// An arbitrary port nothing is listening on.
	resp := doLoopback(&http.Client{}, 1, Frame{ID: "1", Method: "GET", Path: "/api/health"})
	if resp.Error == "" {
		t.Fatal("expected an Error to be set for a failed loopback call")
	}
}

// fakeRelay is a minimal WS server standing in for the real relay: accepts
// one connection, sends a single request Frame, and returns the response
// Frame it gets back -- validates connectOnce's whole wire-level round trip
// (JSON framing, correlation ID passthrough) without needing the real relay
// module.
func fakeRelay(t *testing.T, requestFrame Frame) (addr string, response chan Frame) {
	t.Helper()
	response = make(chan Frame, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		ctx := r.Context()

		data, _ := json.Marshal(requestFrame)
		if err := conn.Write(ctx, websocket.MessageText, data); err != nil {
			return
		}
		_, respData, err := conn.Read(ctx)
		if err != nil {
			return
		}
		var f Frame
		if err := json.Unmarshal(respData, &f); err != nil {
			return
		}
		response <- f
	}))
	t.Cleanup(server.Close)
	return strings.TrimPrefix(server.URL, "http://"), response
}

func TestConnectOnce_RoundTripsARequest(t *testing.T) {
	agentServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("pong"))
	}))
	defer agentServer.Close()
	port, err := strconv.Atoi(strings.Split(agentServer.URL, ":")[2])
	if err != nil {
		t.Fatal(err)
	}

	relayAddr, response := fakeRelay(t, Frame{ID: "42", Method: "GET", Path: "/api/health"})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go func() { _ = connectOnce(ctx, relayAddr, "test-routing-key", port) }()

	select {
	case resp := <-response:
		if resp.ID != "42" {
			t.Fatalf("expected correlation ID 42 preserved, got %q", resp.ID)
		}
		if resp.Status != http.StatusOK {
			t.Fatalf("expected 200, got %d (error=%q)", resp.Status, resp.Error)
		}
		decoded, _ := base64.StdEncoding.DecodeString(resp.Body)
		if string(decoded) != "pong" {
			t.Fatalf("expected body 'pong', got %q", decoded)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for the round-tripped response")
	}
}

func TestIsLocalDomain(t *testing.T) {
	cases := map[string]bool{
		"localhost:8600":        true,
		"127.0.0.1:8600":        true,
		"relay.casperagent.dev": false,
	}
	for domain, want := range cases {
		if got := isLocalDomain(domain); got != want {
			t.Errorf("isLocalDomain(%q) = %v, want %v", domain, got, want)
		}
	}
}

func TestJitter_StaysWithinBounds(t *testing.T) {
	d := 10 * time.Second
	for i := 0; i < 100; i++ {
		j := jitter(d)
		if j < 8*time.Second || j > 12*time.Second {
			t.Fatalf("jitter(%v) = %v, expected within +/-20%%", d, j)
		}
	}
}
