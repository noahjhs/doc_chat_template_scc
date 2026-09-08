package config

import (
	"bytes"
	"encoding/json"
	"net/http"
	"time"
)

// ReportPresence POSTs to the auth service's /presence endpoint, telling it
// where this daemon is currently reachable (its relay URL) and which
// workspace it's confined to. The deployed web app looks this up (GET
// /presence) instead of learning it via query params now that the daemon
// never redirects a browser tab itself. Best-effort: never returns an error
// the caller needs to act on (mirrors RevokeSession's posture) -- a failed
// report just means the web app sees "not connected" until the next one.
func ReportPresence(authDomain, token, localAgentURL, workspace string) {
	body, err := json.Marshal(map[string]string{
		"local_agent_url": localAgentURL,
		"workspace":       workspace,
	})
	if err != nil {
		return
	}
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/presence", bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}

// ClearPresence DELETEs the /presence row -- called on toggle-off and on
// sign-out, using the still-valid token, before it's revoked server-side. A
// race against a caller's own independent /revoke call is possible (whoever
// gets there first), but benign: a lost race just leaves the row to go
// stale until the next pairing overwrites it, or to be read as
// unreachable once the tunnel drops regardless.
func ClearPresence(authDomain, token string) {
	req, err := http.NewRequest(http.MethodDelete, BaseURL(authDomain)+"/presence", nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}
