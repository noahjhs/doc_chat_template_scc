package main

import (
	"net/url"
	"sync"

	"casper-agent/internal/config"
	"casper-agent/internal/server"
	"casper-agent/internal/tunnel"

	"github.com/getlantern/systray"
)

// daemonState holds everything that changes as the daemon moves between its
// three states -- signed out (no session), signed in + off (session valid,
// relay tunnel down), signed in + on (tunnel up) -- and the menu items that
// reflect them. One instance for the process's whole lifetime.
type daemonState struct {
	srv          *server.Server
	relayDomain  string
	authDomain   string
	routingKey   string
	port         int
	workspaceDir string
	logf         func(format string, args ...any)

	mu      sync.Mutex
	tun     *tunnel.Tunnel
	enabled bool
	// The daemon's own copy of the current session token -- kept alongside
	// (not read back from) session.json, so sign-out can still clear
	// presence server-side using it even after server.go's handleShutdown
	// has already deleted that file (ClearSession runs synchronously,
	// before OnSignOut fires -- see server.go's doc comment).
	token string

	// Set once onReady runs; nil until then. A casper:// pairing event can
	// arrive before the menu exists (confirmed via a cold-launch spike: the
	// launch-time Apple Event can be delivered before systray's onReady
	// fires), so every method touching these must tolerate them being nil.
	mToggle  *systray.MenuItem
	mSignOut *systray.MenuItem
}

func newDaemonState(srv *server.Server, relayDomain, authDomain, routingKey string, port int, workspaceDir string, logf func(format string, args ...any)) *daemonState {
	return &daemonState{
		srv:          srv,
		relayDomain:  relayDomain,
		authDomain:   authDomain,
		routingKey:   routingKey,
		port:         port,
		workspaceDir: workspaceDir,
		logf:         logf,
		enabled:      config.LoadEnabled(),
	}
}

func (d *daemonState) ensureTunnel() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.tun != nil {
		return
	}
	t, err := tunnel.Start(d.relayDomain, d.routingKey, d.port)
	if err != nil {
		d.logf("couldn't start relay tunnel: %s", err)
		return
	}
	d.tun = t
}

func (d *daemonState) stopTunnel() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.tun != nil {
		d.tun.Terminate()
		d.tun = nil
	}
}

func (d *daemonState) tunnelURL() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.tun == nil {
		return ""
	}
	return d.tun.URL
}

func (d *daemonState) setToken(token string) {
	d.mu.Lock()
	d.token = token
	d.mu.Unlock()
}

func (d *daemonState) getToken() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.token
}

// setEnabled flips and persists the on/off toggle, starting or stopping the
// relay tunnel to match -- the local HTTP server itself is never touched
// (see internal/server), so /api/health etc. keep responding regardless.
func (d *daemonState) setEnabled(enabled bool) {
	d.mu.Lock()
	d.enabled = enabled
	d.mu.Unlock()
	config.SaveEnabled(enabled)
	if enabled {
		d.ensureTunnel()
		if token := d.getToken(); token != "" {
			go d.reportPresence(token)
		}
	} else {
		d.stopTunnel()
		if token := d.getToken(); token != "" {
			go config.ClearPresence(d.authDomain, token)
		}
	}
	d.applyState()
}

func (d *daemonState) isEnabled() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.enabled
}

// handlePairURL processes a parsed casper://pair?token=...&username=...
// hand-off -- the sole pairing/re-pairing mechanism now that the daemon
// never opens a browser tab itself. No browser interaction, no localhost
// listener: this fully replaces the old loopback-callback pairing dance.
func (d *daemonState) handlePairURL(rawURL string) {
	u, err := url.Parse(rawURL)
	if err != nil {
		d.logf("casper:// URL: couldn't parse %q: %s", rawURL, err)
		return
	}
	if u.Host != "pair" {
		d.logf("casper:// URL: unrecognized action %q", u.Host)
		return
	}
	token := u.Query().Get("token")
	username := u.Query().Get("username")
	if token == "" || username == "" {
		d.logf("casper:// pair URL missing token/username")
		return
	}
	if err := config.SaveSession(username, token); err != nil {
		d.logf("casper:// pair: couldn't save session: %s", err)
	}
	d.setToken(token)
	d.srv.SetAPIKey(token)
	d.logf("Paired as %s", username)
	// Re-pairing always turns the daemon back on -- a user who just went
	// through the sign-in flow expects to end up connected, regardless of
	// whatever the toggle was left at before.
	d.setEnabled(true)
}

// resumeSession is called at startup for a still-valid cached session (see
// main.go) -- sets the in-memory token/API key and re-applies the persisted
// toggle, but (unlike handlePairURL) doesn't force it back on: resuming an
// existing session should honor whatever the user last left the toggle at.
func (d *daemonState) resumeSession(token string) {
	d.setToken(token)
	d.srv.SetAPIKey(token)
	d.setEnabled(d.isEnabled())
}

// onSignOut is wired as the HTTP server's OnSignOut -- called after
// handleShutdown has already cleared session.json and responded to the
// browser. Clears presence using the daemon's own cached token (session.json
// is already gone by this point) and stops the tunnel; the web app's
// sign-out flow revokes the token with the auth service itself, separately.
func (d *daemonState) onSignOut() {
	token := d.getToken()
	d.stopTunnel()
	d.srv.SetAPIKey("")
	d.setToken("")
	if token != "" {
		go config.ClearPresence(d.authDomain, token)
	}
	d.applyState()
}

// signOutFromTray mirrors onSignOut but is triggered locally (the status-bar
// "Sign out" item), where there's no web app in the loop to revoke the
// token server-side -- so this does that part itself, using the daemon's
// cached token before clearing it.
func (d *daemonState) signOutFromTray() {
	token := d.getToken()
	d.stopTunnel()
	d.srv.SetAPIKey("")
	d.setToken("")
	config.ClearSession(d.logf)
	if token != "" {
		go config.ClearPresence(d.authDomain, token)
		go config.RevokeSession(d.authDomain, token)
	}
	d.applyState()
}

func (d *daemonState) reportPresence(token string) {
	tunURL := d.tunnelURL()
	if tunURL == "" {
		return
	}
	config.ReportPresence(d.authDomain, token, tunURL, d.workspaceDir)
}

// applyState brings the status-bar icon/menu in line with the current
// signed-out / signed-in-off / signed-in-on state. Safe to call before the
// menu exists (onReady calls it once after building the menu, to cover a
// pairing event that arrived before that point).
func (d *daemonState) applyState() {
	if d.mToggle == nil || d.mSignOut == nil {
		return
	}
	if !d.srv.HasAPIKey() {
		d.mToggle.Hide()
		d.mSignOut.Hide()
		systray.SetTooltip("Casper — waiting to sign in")
		return
	}
	d.mSignOut.Show()
	d.mToggle.Show()
	if d.isEnabled() {
		d.mToggle.SetTitle("Turn off")
		systray.SetTooltip("Casper — connected")
	} else {
		d.mToggle.SetTitle("Turn on")
		systray.SetTooltip("Casper — paused")
	}
}
