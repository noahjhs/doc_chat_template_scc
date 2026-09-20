package config

import (
	"bytes"
	"encoding/json"
	"net/http"
	"time"
)

// ReportPresence POSTs to the auth service's /hosts/presence endpoint,
// telling it where this daemon is currently reachable (its relay URL) and
// its own confined directory (see commands.Handler.HomeRoot -- a single
// fixed directory, not a dynamic set), authenticated with this
// installation's own device_token. Letting casper_service cache this cwd is
// what lets its own best-effort policy preview do "." path_resolution
// without a live round-trip on every call -- see policy.py's module
// docstring. Returns unauthorized=true when the auth service no longer
// recognizes that token (e.g. after a remote bulk sign-out, or an
// casper_service restart -- its attachment tracking is in-memory, see
// casper_service/main.py's _attached) so the caller can self-heal (clear its
// local session, go idle) instead of retrying forever against a dead
// credential. Otherwise best-effort: a network failure just means the web
// app sees "not connected" until the next successful report.
func ReportPresence(authDomain, deviceToken, localAgentURL, cwd string) (unauthorized bool) {
	body, err := json.Marshal(map[string]any{
		"local_agent_url": localAgentURL,
		"cwd":             cwd,
	})
	if err != nil {
		return false
	}
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/hosts/presence", bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusUnauthorized
}

// ClearPresence DELETEs the /hosts/presence row -- called on toggle-off and
// on sign-out, using the still-valid device_token, before it's unpaired
// server-side. A race against a caller's own independent /hosts/unpair call
// is possible (whoever gets there first), but benign: a lost race just
// leaves the row to go stale until the next presence report overwrites it,
// or to be read as unreachable once the tunnel drops regardless.
func ClearPresence(authDomain, deviceToken string) {
	req, err := http.NewRequest(http.MethodDelete, BaseURL(authDomain)+"/hosts/presence", nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}
