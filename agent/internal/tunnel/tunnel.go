// Package tunnel maintains an outbound WebSocket connection to the relay
// service, standing in for what used to be a per-user Cloudflare quick
// tunnel (a bundled `cloudflared` subprocess). Replaces that implementation
// entirely -- removed, not kept as an opt-in fallback, since a build-tag
// -gated fallback would still need the ~40MB binary present at compile
// time for that build, buying back none of the size savings, for a
// network-compatibility case the relay doesn't actually have (it travels
// through the exact same Cloudflare Tunnel ingress/domain/port that the web
// app and auth service already depend on).
package tunnel

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/coder/websocket"
)

// Frame mirrors relay/internal/wire.Frame exactly -- deliberately
// duplicated rather than shared, matching how auth_service and the
// Streamlit app already have no shared schema module between them.
type Frame struct {
	ID      string            `json:"id"`
	Method  string            `json:"method,omitempty"`
	Path    string            `json:"path,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    string            `json:"body,omitempty"`
	Status  int               `json:"status,omitempty"`
	Error   string            `json:"error,omitempty"`
}

func timeoutFor(path string) time.Duration {
	if path == "/api/command" {
		return 15 * time.Second
	}
	return 5 * time.Second
}

// Tunnel represents the agent's connection to the relay. URL is known
// immediately at Start() -- never scraped from a subprocess's stdout the
// way the old cloudflared-based implementation had to.
type Tunnel struct {
	URL    string
	cancel context.CancelFunc
}

// Start returns immediately with URL already populated (a real, beneficial
// behavior change from the old cloudflared-based version: previously, if
// no tunnel URL showed up within 30s, local_agent_url was never set for the
// rest of that run at all; here a relay blip at launch just means the first
// tool call or two report "Local agent error: ..." until the background
// reconnect below succeeds moments later). Starts a background goroutine
// that dials the relay and keeps it alive, reconnecting with backoff
// forever. Only errors for a malformed relayDomain/routingKey, never for
// network reasons.
func Start(relayDomain, routingKey string, port int) (*Tunnel, error) {
	if relayDomain == "" || routingKey == "" {
		return nil, fmt.Errorf("relay domain and routing key are both required")
	}
	ctx, cancel := context.WithCancel(context.Background())
	httpScheme := "https"
	if isLocalDomain(relayDomain) {
		httpScheme = "http" // matches config.BaseURL's same localhost/127.0.0.1 special-case
	}
	t := &Tunnel{
		URL:    fmt.Sprintf("%s://%s/agent/%s", httpScheme, relayDomain, routingKey),
		cancel: cancel,
	}
	go runReconnectLoop(ctx, relayDomain, routingKey, port)
	return t, nil
}

// Terminate stops the reconnect loop and closes any live connection.
// Nil-safe, matching the old implementation's semantics.
func (t *Tunnel) Terminate() {
	if t != nil && t.cancel != nil {
		t.cancel()
	}
}

func isLocalDomain(domain string) bool {
	return strings.HasPrefix(domain, "localhost") || strings.HasPrefix(domain, "127.0.0.1")
}

// runReconnectLoop keeps trying to (re)connect forever, with exponential
// backoff (1s doubling to a 30s cap, +/-20% jitter to avoid a thundering
// herd if the relay bounces and many agents reconnect at once), reset to
// the base delay only once a connection has stayed up for a while --
// avoids a flapping connection perpetually resetting to instant retries.
func runReconnectLoop(ctx context.Context, relayDomain, routingKey string, port int) {
	const (
		baseDelay   = 1 * time.Second
		maxDelay    = 30 * time.Second
		stableAfter = 10 * time.Second
	)
	delay := baseDelay
	for ctx.Err() == nil {
		start := time.Now()
		_ = connectOnce(ctx, relayDomain, routingKey, port)
		if ctx.Err() != nil {
			return
		}
		if time.Since(start) > stableAfter {
			delay = baseDelay
		} else {
			delay *= 2
			if delay > maxDelay {
				delay = maxDelay
			}
		}
		select {
		case <-time.After(jitter(delay)):
		case <-ctx.Done():
			return
		}
	}
}

func jitter(d time.Duration) time.Duration {
	delta := float64(d) * 0.2
	offset := (rand.Float64()*2 - 1) * delta
	return time.Duration(float64(d) + offset)
}

// connectOnce dials the relay, then serves it until the connection breaks
// (in either direction) or ctx is cancelled, returning the resulting error.
func connectOnce(parentCtx context.Context, relayDomain, routingKey string, port int) error {
	ctx, cancel := context.WithCancel(parentCtx)
	defer cancel()

	scheme := "wss"
	if isLocalDomain(relayDomain) {
		scheme = "ws" // mirrors config.BaseURL's localhost/127.0.0.1 special-case elsewhere in this codebase
	}
	u := url.URL{Scheme: scheme, Host: relayDomain, Path: "/connect", RawQuery: "key=" + url.QueryEscape(routingKey)}

	conn, _, err := websocket.Dial(parentCtx, u.String(), nil)
	if err != nil {
		return err
	}
	defer conn.CloseNow()

	outbound := make(chan Frame, 16)

	go func() {
		// If the writer ever exits (a write error, or ctx already done),
		// cancel so the blocking Read below unblocks promptly instead of
		// this connection lingering half-dead.
		defer cancel()
		for {
			select {
			case frame, ok := <-outbound:
				if !ok {
					return
				}
				data, err := json.Marshal(frame)
				if err != nil {
					continue // malformed frame -- drop rather than wedge the writer
				}
				if err := conn.Write(ctx, websocket.MessageText, data); err != nil {
					return
				}
			case <-ctx.Done():
				return
			}
		}
	}()

	client := &http.Client{}
	for {
		_, data, err := conn.Read(ctx)
		if err != nil {
			return err
		}
		var frame Frame
		if err := json.Unmarshal(data, &frame); err != nil {
			continue // malformed frame from the relay -- ignore, not fatal to the connection
		}
		go func(frame Frame) {
			resp := doLoopback(client, port, frame)
			select {
			case outbound <- resp:
			case <-ctx.Done():
			}
		}(frame)
	}
}

// doLoopback dispatches one relayed request to the agent's own
// already-running server (agent/internal/server) via a plain loopback HTTP
// call -- reuses that server completely unchanged.
func doLoopback(client *http.Client, port int, frame Frame) Frame {
	var bodyReader io.Reader
	if frame.Body != "" {
		decoded, err := base64.StdEncoding.DecodeString(frame.Body)
		if err != nil {
			return Frame{ID: frame.ID, Error: "malformed request body"}
		}
		bodyReader = bytes.NewReader(decoded)
	}

	reqCtx, cancel := context.WithTimeout(context.Background(), timeoutFor(frame.Path))
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, frame.Method, fmt.Sprintf("http://localhost:%d%s", port, frame.Path), bodyReader)
	if err != nil {
		return Frame{ID: frame.ID, Error: err.Error()}
	}
	for k, v := range frame.Headers {
		req.Header.Set(k, v)
	}

	resp, err := client.Do(req)
	if err != nil {
		return Frame{ID: frame.ID, Error: err.Error()}
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return Frame{ID: frame.ID, Error: err.Error()}
	}

	headers := map[string]string{}
	if ct := resp.Header.Get("Content-Type"); ct != "" {
		headers["Content-Type"] = ct
	}
	return Frame{
		ID:      frame.ID,
		Status:  resp.StatusCode,
		Headers: headers,
		Body:    base64.StdEncoding.EncodeToString(respBody),
	}
}
