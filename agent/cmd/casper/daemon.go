package main

import (
	"errors"
	"net/url"
	"sync"
	"time"

	"casper-agent/internal/activate"
	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/server"
	"casper-agent/internal/tunnel"

	"github.com/getlantern/systray"
)

// daemonState holds everything that changes as the daemon moves between its
// three states -- signed out (no session), signed in + off (session valid,
// relay tunnel down), signed in + on (tunnel up) -- and the menu items that
// reflect them. One instance for the process's whole lifetime.
type daemonState struct {
	srv         *server.Server
	cmdHandler  *commands.Handler // for Roots() -- presence reports the *current* set, which can grow at runtime (see commands.Handler.AddRoot)
	relayDomain string
	authDomain  string
	routingKey  string
	port        int
	logf        func(format string, args ...any)

	mu      sync.Mutex
	tun     *tunnel.Tunnel
	enabled bool
	// The daemon's own copy of its current host credentials -- kept
	// alongside (not read back from) session.json, so sign-out can still
	// clear presence/unpair server-side using them even after server.go's
	// handleShutdown has already deleted that file (ClearSession runs
	// synchronously, before OnSignOut fires -- see server.go's doc
	// comment). deviceToken authenticates to the auth service (presence,
	// verify, unpair); commandKey authenticates the browser to this
	// daemon's own /api/command -- see internal/config/hostpair.go.
	deviceToken string
	commandKey  string

	// Set once onReady runs; nil until then. A casper:// pairing event can
	// arrive before the menu exists (confirmed via a cold-launch spike: the
	// launch-time Apple Event can be delivered before systray's onReady
	// fires), so every method touching these must tolerate them being nil.
	mToggle  *systray.MenuItem
	mSignOut *systray.MenuItem
}

func newDaemonState(srv *server.Server, cmdHandler *commands.Handler, relayDomain, authDomain, routingKey string, port int, logf func(format string, args ...any)) *daemonState {
	return &daemonState{
		srv:         srv,
		cmdHandler:  cmdHandler,
		relayDomain: relayDomain,
		authDomain:  authDomain,
		routingKey:  routingKey,
		port:        port,
		logf:        logf,
		enabled:     config.LoadEnabled(),
	}
}

// reportPresenceNow is the on-demand counterpart to setEnabled's own
// presence push -- called whenever the set of addressable directories
// itself changes (see commands.Handler's onRootAdded, wired in main.go),
// so the web app's workspace browser doesn't have to wait for some other
// trigger to see a just-added directory. A no-op while signed out.
func (d *daemonState) reportPresenceNow() {
	if deviceToken := d.getDeviceToken(); deviceToken != "" {
		go d.reportPresence(deviceToken)
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

func (d *daemonState) setCredentials(deviceToken, commandKey string) {
	d.mu.Lock()
	d.deviceToken = deviceToken
	d.commandKey = commandKey
	d.mu.Unlock()
}

func (d *daemonState) getDeviceToken() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.deviceToken
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
		if deviceToken := d.getDeviceToken(); deviceToken != "" {
			go d.reportPresence(deviceToken)
		}
	} else {
		d.stopTunnel()
		if deviceToken := d.getDeviceToken(); deviceToken != "" {
			go config.ClearPresence(d.authDomain, deviceToken)
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
// The URL's token is now explicitly a one-time bootstrap value -- see
// internal/config/hostpair.go -- exchanged here for this installation's own
// independent device_token/command_key before anything else happens.
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
	bootstrapToken := u.Query().Get("token")
	username := u.Query().Get("username")
	if bootstrapToken == "" || username == "" {
		d.logf("casper:// pair URL missing token/username")
		return
	}
	deviceToken, commandKey, _, err := config.ExchangePairingToken(d.authDomain, bootstrapToken, d.routingKey)
	if err != nil {
		if errors.Is(err, config.ErrHostConflict) {
			d.logf("casper:// pair: this host is already attached to another account")
			dialog.ShowError("This machine is already attached to another Casper account. Sign out there first, or pair a different machine.")
		} else {
			d.logf("casper:// pair: couldn't exchange pairing token: %s", err)
		}
		return
	}
	if err := config.SaveSession(username, deviceToken, commandKey); err != nil {
		d.logf("casper:// pair: couldn't save session: %s", err)
	}
	d.setCredentials(deviceToken, commandKey)
	d.srv.SetAPIKey(commandKey)
	d.logf("Paired as %s", username)
	// Re-pairing always turns the daemon back on -- a user who just went
	// through the sign-in flow expects to end up connected, regardless of
	// whatever the toggle was left at before.
	d.setEnabled(true)
	// Re-foregrounds the browser -- see internal/activate's doc comment for
	// why this exists (a JS-only fix from the browser side didn't hold up).
	// A short delay first, off the goroutine handling this event, so it
	// doesn't compete with whatever's still settling right after the OS
	// just delivered this Apple Event (and so a slow/failing activation
	// attempt can never block pairing itself).
	go func() {
		time.Sleep(300 * time.Millisecond)
		activate.DefaultBrowser()
		d.logf("Attempted to re-foreground the default browser")
	}()
}

// resumeSession is called at startup for a still-valid cached session (see
// main.go) -- sets the in-memory credentials/API key and re-applies the
// persisted toggle, but (unlike handlePairURL) doesn't force it back on:
// resuming an existing session should honor whatever the user last left the
// toggle at.
func (d *daemonState) resumeSession(deviceToken, commandKey string) {
	d.setCredentials(deviceToken, commandKey)
	d.srv.SetAPIKey(commandKey)
	d.setEnabled(d.isEnabled())
}

// onSignOut is wired as the HTTP server's OnSignOut -- called after
// handleShutdown has already cleared session.json and responded to the
// browser. Clears presence using the daemon's own cached device_token
// (session.json is already gone by this point) and stops the tunnel; the
// web app's sign-out flow unpairs the host with the auth service itself,
// separately (see auth_service's /hosts/signout-all).
func (d *daemonState) onSignOut() {
	deviceToken := d.getDeviceToken()
	d.stopTunnel()
	d.srv.SetAPIKey("")
	d.setCredentials("", "")
	if deviceToken != "" {
		go config.ClearPresence(d.authDomain, deviceToken)
	}
	d.applyState()
}

// signOutFromTray mirrors onSignOut but is triggered locally (the status-bar
// "Sign out" item), where there's no web app in the loop to unpair the host
// server-side -- so this does that part itself, using the daemon's cached
// device_token before clearing it.
func (d *daemonState) signOutFromTray() {
	deviceToken := d.getDeviceToken()
	d.stopTunnel()
	d.srv.SetAPIKey("")
	d.setCredentials("", "")
	config.ClearSession(d.logf)
	if deviceToken != "" {
		go config.ClearPresence(d.authDomain, deviceToken)
		go config.UnpairHost(d.authDomain, deviceToken)
	}
	d.applyState()
}

// reportPresence self-heals on a 401: the auth service no longer
// recognizing this device_token (a remote sign-out, or an auth_service
// restart clearing its in-memory attachment map -- see
// auth_service/main.py's _attached) means this daemon's session is
// unrecoverable, so it clears its own state and goes idle rather than
// retrying forever against a dead credential.
func (d *daemonState) reportPresence(deviceToken string) {
	tunURL := d.tunnelURL()
	if tunURL == "" {
		return
	}
	if unauthorized := config.ReportPresence(d.authDomain, deviceToken, tunURL, d.cmdHandler.Roots()); unauthorized {
		d.logf("Device session no longer recognized by the auth service -- signing out locally")
		d.stopTunnel()
		d.srv.SetAPIKey("")
		d.setCredentials("", "")
		config.ClearSession(d.logf)
		d.applyState()
	}
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
