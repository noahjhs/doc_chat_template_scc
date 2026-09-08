// Package pairing implements the first-run sign-in flow: a local loopback
// HTTP listener the deployed web app's /signin page calls back into with a
// token, plus the "reuse a cached session, or fall through to a fresh
// sign-in" orchestration. A port of casper_tool.py's
// open_pairing_page_and_wait()/PAIRING_SPINNER_HTML/ensure_authenticated()/
// start_and_open_chat().
package pairing

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"

	"casper-agent/internal/browser"
	"casper-agent/internal/config"
	"casper-agent/internal/tunnel"
)

// A complete, ordinary standalone page -- sent as soon as the sign-in
// callback lands, before the Cloudflare tunnel has actually come up, so the
// tab shows something rather than a bare pending request for however long
// that takes. This page's own JS polls /status on this same local listener
// for when the tunnel's ready, redirecting itself once it is.
const pairingSpinnerHTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>Casper</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex;
         flex-direction: column; align-items: center; justify-content: center;
         height: 100vh; margin: 0; gap: 1rem; color: #333; }
  .spinner { width: 32px; height: 32px; border: 4px solid #ddd;
             border-top-color: #555; border-radius: 50%;
             animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style></head>
<body>
<div class="spinner"></div>
<p>Signed in — connecting to Casper…</p>
<script>
function poll() {
    fetch("/status")
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ready && data.chat_url) {
                window.location.href = data.chat_url;
            } else {
                setTimeout(poll, 300);
            }
        })
        .catch(function() { setTimeout(poll, 300); });
}
poll();
</script>
</body></html>
`

func randomToken(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// OpenPairingPageAndWait opens the deployed app's /signin page in a real
// browser tab and waits on a short-lived local HTTP listener for it to hand
// back a token -- the same "loopback" pattern CLI tools like gcloud or AWS
// SSO use for browser-based sign-in.
//
// Binds to `port` (the same port the main API server uses once pairing
// completes -- they never run at the same time) rather than a random one, so
// both phases of this one run share a single, predictable port.
//
// onAuthenticated(username, token) runs in a background goroutine once that
// callback arrives; its return value is the resulting chat URL. This
// function blocks until that completes (or timeout elapses) before
// returning, since callers need its result (e.g. the tunnel) available by
// the time it does -- it just keeps answering the page's /status polls
// meanwhile instead of sitting on the one request.
func OpenPairingPageAndWait(appDomain string, port int, browserName string, timeout time.Duration, onAuthenticated func(username, token string) string) error {
	nonce, err := randomToken(16)
	if err != nil {
		return err
	}
	pairURL := fmt.Sprintf("%s/signin?callback_port=%d&nonce=%s", config.BaseURL(appDomain), port, url.QueryEscape(nonce))

	var mu sync.Mutex
	var chatURL string
	authDone := make(chan struct{})
	var closeOnce sync.Once

	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		// Polled by pairingSpinnerHTML's own JS -- same-origin (that page
		// came from this same listener), no CORS needed.
		mu.Lock()
		ready := chatURL != ""
		url := chatURL
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ready": ready, "chat_url": url})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("nonce") != nonce || q.Get("token") == "" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		username := q.Get("username")
		token := q.Get("token")
		go func() {
			result := onAuthenticated(username, token)
			mu.Lock()
			chatURL = result
			mu.Unlock()
			// Give the spinner page's own polling JS (300ms interval) a
			// real chance to see chat_url ready via /status before this
			// listener closes (OpenPairingPageAndWait returns as soon as
			// authDone fires, and its deferred srv.Close() tears down this
			// exact port). With the relay-based tunnel, onAuthenticated now
			// completes almost instantly instead of taking up to 30s (the
			// old cloudflared-scraping wait) -- fast enough that, without
			// this pause, authDone can fire and close the server before
			// the browser has even finished painting the spinner page and
			// firing its first /status fetch, let alone gotten a chance to
			// see it succeed -- reported directly as a hang on that
			// spinner. A synthetic same-process test can't reliably
			// reproduce this exact race (a local Go HTTP round-trip is far
			// faster than real page-load + JS-startup latency, so it
			// passes with or without this line) -- the real evidence is
			// the reported hang itself, not a passing test.
			time.Sleep(2 * time.Second)
			closeOnce.Do(func() { close(authDone) })
		}()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(pairingSpinnerHTML))
	})

	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		// The overwhelmingly common cause: an earlier Casper launch is
		// still running in the background, still holding this port --
		// easy to end up with unnoticed now that there's no console/Dock
		// window to make a prior instance's continued existence obvious.
		return fmt.Errorf(
			"Casper couldn't start (port %d is already in use). "+
				"Casper may already be running -- quit it first (check Activity "+
				"Monitor for another \"Casper\" process) and try again. (%s)",
			port, err,
		)
	}
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(listener) }()
	defer srv.Close()

	fmt.Printf("Sign in to continue: %s\n", pairURL)
	_ = browser.OpenURL(pairURL, browserName)

	select {
	case <-authDone:
		return nil
	case <-time.After(timeout):
		return fmt.Errorf("timed out waiting for sign-in — run the tool again to retry")
	}
}

// StartAndOpenChat connects to the relay (a random routing key generated
// fresh each run -- see the package docs on why this is deliberately not
// the real API key) and builds the chat app's URL, passing the resulting
// relay URL, API key, and the confined workspace directory along as query
// params so the chat page connects automatically. If openNewTab, also opens
// it in a new browser tab -- pass false when the caller will instead
// redirect an already-open tab there itself (e.g. the /signin tab, once
// pairing completes).
func StartAndOpenChat(appDomain, relayDomain, apiKey string, port int, workspaceDir string, openNewTab bool, browserName string) (*tunnel.Tunnel, string) {
	routingKey, err := randomToken(24)
	if err != nil {
		fmt.Printf("Couldn't generate a routing key: %s\n", err)
	}
	t, _ := tunnel.Start(relayDomain, routingKey, port)

	appURL := config.BuildAppURL(appDomain)
	openURL := appURL
	// t.URL is known immediately (unlike the old cloudflared-scraped URL,
	// which could be genuinely absent if no tunnel came up in time) -- set
	// unconditionally now; a relay connection blip just means the first
	// tool call or two report a local-agent error until the background
	// reconnect (see internal/tunnel) succeeds.
	if t != nil {
		if parsed, err := url.Parse(appURL); err == nil {
			q := parsed.Query()
			q.Set("local_agent_url", t.URL)
			q.Set("local_agent_token", apiKey)
			q.Set("local_agent_workspace", workspaceDir)
			parsed.RawQuery = q.Encode()
			openURL = parsed.String()
		}
	}

	if openNewTab {
		if err := browser.OpenURL(openURL, browserName); err != nil {
			fmt.Printf("Couldn't open a browser tab for %s: %s\n", openURL, err)
		} else {
			fmt.Printf("🌍 Opened %s in your browser.\n", openURL)
		}
	}
	return t, openURL
}

// EnsureAuthenticated returns (apiKey, tunnel), prompting a fresh sign-in
// only when needed: a cached session is reused silently if it still
// verifies (and the tunnel/chat tab are started right away, in a new tab);
// a network hiccup while checking it doesn't block startup (trust the
// cached token rather than force a re-login just because the auth service
// was slow to answer); an explicitly invalid/absent session falls through
// to a fresh browser-based sign-in, in which case the tunnel only starts
// once that completes, and the same tab that showed /signin is redirected
// straight to the chat app rather than a new tab being opened for it.
func EnsureAuthenticated(
	appDomain, authDomain, relayDomain string, port int, workspaceDir, browserName string,
	logf func(format string, args ...any),
) (string, *tunnel.Tunnel, error) {
	session, err := config.LoadSession()
	if err != nil {
		return "", nil, err
	}
	if session != nil {
		result := config.VerifySession(authDomain, session.Token)
		if result != config.VerifyInvalid { // valid, or unknown (couldn't check -- trust it)
			if result == config.VerifyUnknown {
				logf("Couldn't verify saved session (offline?) -- using it anyway.")
			}
			t, _ := StartAndOpenChat(appDomain, relayDomain, session.Token, port, workspaceDir, true, browserName)
			return session.Token, t, nil
		}
		// Cleared right away, not just left for /api/shutdown's cleanup --
		// that runs at sign-out time, but this is the moment a *stale*
		// session (e.g. from a run that ended some other way) actually
		// gets confirmed dead. Without this, a session stuck in that state
		// prints this same message on every single launch rather than at
		// most once.
		config.ClearSession(logf)
		logf("Saved session is no longer valid -- signing in again.")
	}

	var resultToken string
	var resultTunnel *tunnel.Tunnel
	onAuthenticated := func(username, token string) string {
		_ = config.SaveSession(username, token)
		logf("Signed in as %s", username)
		t, chatURL := StartAndOpenChat(appDomain, relayDomain, token, port, workspaceDir, false, "")
		resultToken = token
		resultTunnel = t
		return chatURL
	}
	if err := OpenPairingPageAndWait(appDomain, port, browserName, 5*time.Minute, onAuthenticated); err != nil {
		return "", nil, err
	}
	return resultToken, resultTunnel, nil
}
