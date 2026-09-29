// Package wire defines the JSON frame protocol exchanged between the relay
// and each connected agent over its WebSocket connection. This struct is
// deliberately duplicated (not shared via a common module) in
// agent/internal/tunnel -- matches how casper_service and the Streamlit app
// already have no shared schema module between them.
package wire

import "time"

// Frame is the single message type used in both directions. Method/Path are
// relay -> agent only; Status/Error are agent -> relay only; ID/Headers/Body
// are used both ways.
type Frame struct {
	ID      string            `json:"id"`
	Method  string            `json:"method,omitempty"`
	Path    string            `json:"path,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    string            `json:"body,omitempty"` // base64 standard encoding
	Status  int               `json:"status,omitempty"`
	Error   string            `json:"error,omitempty"`
}

// MaxFrameBytes is the largest single WebSocket message either end will
// read -- coder/websocket's own default is only 32KB, which silently broke
// any tunneled request/response bigger than that (large command output, and
// every backup chunk: 4MB of ciphertext is ~7.1MB once base64'd into the
// request's JSON body and then again into Frame.Body). Mirrored by
// agent/internal/tunnel's own maxFrameBytes.
const MaxFrameBytes = 16 << 20

// TimeoutFor returns how long the relay should wait for an agent's response
// to a given request path before giving up -- matches pages/chat.py's exact
// existing client-side timeouts (call_local_agent's 15s for /api/command,
// the sign-out flow's 5s for /api/shutdown) so a relay timeout and the web
// app's own timeout don't race each other unpredictably.
func TimeoutFor(path string) time.Duration {
	if path == "/api/command" {
		return 15 * time.Second
	}
	return 5 * time.Second
}
