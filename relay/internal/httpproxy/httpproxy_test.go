package httpproxy

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"casper-relay/internal/registry"
	"casper-relay/internal/wire"
)

func testLogger() *log.Logger {
	return log.New(io.Discard, "", 0)
}

// shortTimeout stands in for wire.TimeoutFor in tests -- a few milliseconds
// instead of the real 5s/15s, so the timeout test doesn't block for real.
func shortTimeout(string) time.Duration { return 20 * time.Millisecond }

type fakeConn struct {
	resp  wire.Frame
	err   error
	block chan struct{}
}

func (f *fakeConn) Do(ctx context.Context, _ wire.Frame) (wire.Frame, error) {
	if f.block != nil {
		select {
		case <-f.block:
		case <-ctx.Done():
			return wire.Frame{}, ctx.Err()
		}
	}
	if f.err != nil {
		return wire.Frame{}, f.err
	}
	return f.resp, nil
}

func (f *fakeConn) Close() {}

// newTestMux mirrors cmd/relay/main.go's route registration -- needed so
// r.PathValue("key")/r.PathValue("rest") are actually populated (Go's
// ServeMux only fills those in when a request is dispatched through a real
// mux match, not when a handler func is called directly).
func newTestMux(reg *registry.Registry) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/agent/{key}/{rest...}", Handler(reg, testLogger(), shortTimeout))
	return mux
}

func doRequest(mux *http.ServeMux, method, path string, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	return rec
}

func TestHandler_NoConnectionRegistered(t *testing.T) {
	reg := registry.New()
	mux := newTestMux(reg)

	rec := doRequest(mux, "POST", "/agent/missing-key/api/command", `{"action":"pwd"}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestHandler_HappyPath(t *testing.T) {
	reg := registry.New()
	respBody := base64.StdEncoding.EncodeToString([]byte(`{"success":true,"cwd":"/x","stdout":"/x","stderr":""}`))
	conn := &fakeConn{resp: wire.Frame{Status: 200, Body: respBody, Headers: map[string]string{"Content-Type": "application/json"}}}
	reg.Register("key1", conn)
	mux := newTestMux(reg)

	rec := doRequest(mux, "POST", "/agent/key1/api/command", `{"action":"pwd"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"cwd":"/x"`) {
		t.Fatalf("expected proxied body, got %s", rec.Body.String())
	}
}

func TestHandler_AgentTimeout(t *testing.T) {
	reg := registry.New()
	conn := &fakeConn{block: make(chan struct{})} // never unblocks -- forces the context timeout path
	reg.Register("key1", conn)
	mux := newTestMux(reg)

	rec := doRequest(mux, "POST", "/agent/key1/api/shutdown", "")
	if rec.Code != http.StatusGatewayTimeout {
		t.Fatalf("expected 504, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestHandler_AgentLoopbackError(t *testing.T) {
	reg := registry.New()
	conn := &fakeConn{resp: wire.Frame{Error: "connection refused"}}
	reg.Register("key1", conn)
	mux := newTestMux(reg)

	rec := doRequest(mux, "POST", "/agent/key1/api/command", `{"action":"pwd"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestHandler_ConnDoError(t *testing.T) {
	reg := registry.New()
	conn := &fakeConn{err: errors.New("boom")}
	reg.Register("key1", conn)
	mux := newTestMux(reg)

	rec := doRequest(mux, "POST", "/agent/key1/api/command", `{"action":"pwd"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d: %s", rec.Code, rec.Body.String())
	}
}
