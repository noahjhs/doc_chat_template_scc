package main

import (
	"fmt"
	"net/url"
	"sync"
	"time"

	"casper-agent/internal/activate"
	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/server"
	"casper-agent/internal/tunnel"

	"github.com/getlantern/systray"
)

// pairedIdentity is one currently-paired Casper account's own independent
// state -- its own credentials and its own commands.Handler (so its own
// cached policy layers, fully isolated from every other identity even
// though homeRoot is the same underlying directory for all of them -- see
// server.go's identity type, the HTTP-layer counterpart that only needs
// the handler/callbacks, not username/deviceToken).
type pairedIdentity struct {
	username    string
	deviceToken string
	commandKey  string
	handler     *commands.Handler
}

// daemonState holds everything that changes as the daemon moves between its
// three coarse states -- signed out (no identities), signed in + off
// (at least one identity, relay tunnel down), signed in + on (tunnel up) --
// and the menu items that reflect them. One instance for the process's
// whole lifetime. The relay tunnel is genuinely machine-level (see
// tunnel.go/relay's own dumb-pipe-by-routing_key design) so it stays a
// single shared resource here regardless of how many identities are
// paired; everything identity-specific lives in the identities map below.
type daemonState struct {
	srv         *server.Server
	homeRoot    string // shared by every identity's own commands.Handler -- see commands.New
	relayDomain string
	authDomain  string
	routingKey  string
	port        int
	logf        func(format string, args ...any)

	mu         sync.Mutex
	tun        *tunnel.Tunnel
	enabled    bool
	identities map[string]*pairedIdentity // keyed by username

	// Set once onReady runs; nil until then. A casper:// pairing event can
	// arrive before the menu exists (confirmed via a cold-launch spike: the
	// launch-time Apple Event can be delivered before systray's onReady
	// fires), so every method touching these must tolerate them being nil.
	mToggle *systray.MenuItem
	mStatus *systray.MenuItem
}

func newDaemonState(
	srv *server.Server, homeRoot, relayDomain, authDomain, routingKey string, port int,
	logf func(format string, args ...any),
) *daemonState {
	return &daemonState{
		srv:         srv,
		homeRoot:    homeRoot,
		relayDomain: relayDomain,
		authDomain:  authDomain,
		routingKey:  routingKey,
		port:        port,
		logf:        logf,
		enabled:     config.LoadEnabled(),
		identities:  make(map[string]*pairedIdentity),
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

// sessionSnapshotLocked must be called with d.mu already held.
func (d *daemonState) sessionSnapshotLocked() []config.Session {
	sessions := make([]config.Session, 0, len(d.identities))
	for _, pi := range d.identities {
		sessions = append(sessions, config.Session{Username: pi.username, DeviceToken: pi.deviceToken, CommandKey: pi.commandKey})
	}
	return sessions
}

func (d *daemonState) deviceTokenFor(username string) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	if pi, ok := d.identities[username]; ok {
		return pi.deviceToken
	}
	return ""
}

func (d *daemonState) identityCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.identities)
}

// addOrReplaceIdentity registers a freshly (re-)paired account, live -- a
// brand-new username gets its own fresh commands.Handler; re-pairing an
// already-known username (rotating credentials, e.g. reconnecting) reuses
// its existing Handler so its already-cached policy layers don't
// momentarily disappear while a fresh fetch is in flight. Persists the
// full updated session list and (re)starts this identity's own
// policy-layer fetch.
func (d *daemonState) addOrReplaceIdentity(username, deviceToken, commandKey string) {
	d.mu.Lock()
	old := d.identities[username]
	var handler *commands.Handler
	if old != nil {
		handler = old.handler
	} else {
		handler = commands.New(d.homeRoot)
	}
	pi := &pairedIdentity{username: username, deviceToken: deviceToken, commandKey: commandKey, handler: handler}
	d.identities[username] = pi
	snapshot := d.sessionSnapshotLocked()
	d.mu.Unlock()

	handler.SetRefreshPolicyLayersFunc(func() {
		if dt := d.deviceTokenFor(username); dt != "" {
			d.refreshPolicyLayers(dt, handler)
		}
	})

	if old != nil && old.commandKey != commandKey {
		d.srv.RemoveIdentity(old.commandKey)
	}
	d.srv.AddIdentity(commandKey, handler,
		func() { d.removeSessionFromDisk(username) },
		func() { d.finishSignOut(username, commandKey, deviceToken) },
	)

	if err := config.SaveSessions(snapshot); err != nil {
		d.logf("casper:// pair: couldn't save session: %s", err)
	}
	go d.refreshPolicyLayers(deviceToken, handler)
}

// removeSessionFromDisk is the synchronous half of signing out one identity
// -- wired as that identity's server-level clearSession callback, run
// before the /api/shutdown response is sent (see server.go's doc comment
// on why this half is synchronous: the token is also being revoked
// server-side right now, so there's nothing left the file could still be
// useful for). Drops it from the in-memory map too, at the same point --
// cheap, local, no reason to defer that part to the async half.
func (d *daemonState) removeSessionFromDisk(username string) {
	d.mu.Lock()
	delete(d.identities, username)
	snapshot := d.sessionSnapshotLocked()
	d.mu.Unlock()
	if err := config.SaveSessions(snapshot); err != nil {
		d.logf("sign-out: couldn't update session file: %s", err)
	}
}

// finishSignOut is the async half -- run after the /api/shutdown response
// is already on the wire (or directly by removeIdentity for a path with no
// HTTP response to sequence against, e.g. reportPresence's self-heal).
// Deregisters this identity from the server, stops the relay tunnel only
// if it was the LAST paired identity (the tunnel is machine-level, shared
// by every other identity still paired here), and clears this identity's
// own presence.
func (d *daemonState) finishSignOut(username, commandKey, deviceToken string) {
	d.srv.RemoveIdentity(commandKey)
	if d.identityCount() == 0 {
		d.stopTunnel()
	}
	if deviceToken != "" {
		go config.ClearPresence(d.authDomain, deviceToken)
	}
	d.logf("Signed out %s", username)
	d.applyState()
}

// removeIdentity fully signs out one identity in one call -- for callers
// outside the HTTP-triggered sign-out flow (e.g. reportPresence's self-heal
// on a 401, where there's no separate response to sequence the sync/async
// halves against).
func (d *daemonState) removeIdentity(username string) {
	d.mu.Lock()
	pi, ok := d.identities[username]
	if !ok {
		d.mu.Unlock()
		return
	}
	delete(d.identities, username)
	snapshot := d.sessionSnapshotLocked()
	d.mu.Unlock()

	if err := config.SaveSessions(snapshot); err != nil {
		d.logf("sign-out: couldn't update session file: %s", err)
	}
	d.finishSignOut(pi.username, pi.commandKey, pi.deviceToken)
}

// dropUnresumedSession removes username from the persisted session list
// without ever having added it to d.identities -- used at startup for a
// cached session that fails verification (config.VerifyInvalid): it was
// never resumed, so removeIdentity's map-based lookup wouldn't find it.
func (d *daemonState) dropUnresumedSession(username string) {
	sessions, err := config.LoadSessions()
	if err != nil {
		return
	}
	filtered := make([]config.Session, 0, len(sessions))
	for _, s := range sessions {
		if s.Username != username {
			filtered = append(filtered, s)
		}
	}
	if err := config.SaveSessions(filtered); err != nil {
		d.logf("couldn't drop invalid session for %s: %s", username, err)
	}
}

// setEnabled flips and persists the on/off toggle, starting or stopping the
// relay tunnel to match -- the local HTTP server itself is never touched
// (see internal/server), so /api/health etc. keep responding regardless.
// Fans presence reporting/clearing out to every currently-paired identity.
func (d *daemonState) setEnabled(enabled bool) {
	d.mu.Lock()
	d.enabled = enabled
	identities := make([]*pairedIdentity, 0, len(d.identities))
	for _, pi := range d.identities {
		identities = append(identities, pi)
	}
	d.mu.Unlock()
	config.SaveEnabled(enabled)
	if enabled {
		d.ensureTunnel()
		for _, pi := range identities {
			go d.reportPresence(pi.username, pi.deviceToken)
		}
	} else {
		d.stopTunnel()
		for _, pi := range identities {
			go config.ClearPresence(d.authDomain, pi.deviceToken)
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
// Pairing a username already paired here rotates its credentials in place
// (addOrReplaceIdentity); pairing a NEW username adds it alongside every
// other currently-paired account -- one physical machine can be signed
// into more than one Casper account at once, each fully isolated.
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
		d.logf("casper:// pair: couldn't exchange pairing token: %s", err)
		config.SaveLastPairingResult("error", err.Error())
		return
	}
	d.addOrReplaceIdentity(username, deviceToken, commandKey)
	d.logf("Paired as %s", username)
	config.SaveLastPairingResult("ok", fmt.Sprintf("Paired as %s", username))
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

// resumeIdentity is called at startup for each still-valid cached session
// (see main.go) -- registers the identity and re-applies the persisted
// toggle, but (unlike handlePairURL) doesn't force it back on: resuming an
// existing session should honor whatever the user last left the toggle at.
func (d *daemonState) resumeIdentity(s config.Session) {
	d.addOrReplaceIdentity(s.Username, s.DeviceToken, s.CommandKey)
	d.setEnabled(d.isEnabled())
}

// refreshPolicyLayers fetches one identity's own enabled policy layers from
// the auth service and replaces its Handler's cached copy (see
// commands.Handler.SetPolicyLayers) -- called after pairing and after
// resuming a cached session, both natural points credentials become
// available, plus on demand via the "refresh_policy_layers" action (not
// model-visible, same posture as add_directory -- only the web app's own
// policy-authoring UI triggers it) so an edit made there doesn't wait for
// the next pairing/restart to take effect. Best-effort: a fetch failure
// just leaves whatever was cached before in place (or empty, on first
// fetch) -- v1 has no push/websocket mechanism, so a stale cache only
// self-heals on the next of these three triggers.
func (d *daemonState) refreshPolicyLayers(deviceToken string, handler *commands.Handler) {
	layers, err := config.FetchPolicyLayers(d.authDomain, deviceToken)
	if err != nil {
		d.logf("Couldn't fetch policy layers: %s", err)
		return
	}
	handler.SetPolicyLayers(layers)
}

// reportPresence self-heals on a 401: the auth service no longer
// recognizing this device_token (a remote sign-out, or an auth_service
// restart clearing its in-memory attachment map -- see
// auth_service/main.py's _attached) means this one identity's session is
// unrecoverable, so it signs out just that identity and goes idle rather
// than retrying forever against a dead credential -- every OTHER identity
// still paired here is untouched.
func (d *daemonState) reportPresence(username, deviceToken string) {
	tunURL := d.tunnelURL()
	if tunURL == "" {
		return
	}
	if unauthorized := config.ReportPresence(d.authDomain, deviceToken, tunURL, d.homeRoot); unauthorized {
		d.logf("Device session for %s no longer recognized by the auth service -- signing out locally", username)
		d.removeIdentity(username)
	}
}

// applyState brings the status-bar icon/menu in line with the current
// running/paused (isEnabled) state. Safe to call before the menu exists
// (onReady calls it once after building the menu, to cover a pairing event
// that arrived before that point).
//
// Deliberately independent of sign-in state (d.srv.HasAPIKey()) -- an
// earlier version hid mToggle/mStatus entirely while signed out, which
// just reads as "the status item is missing" (confirmed directly: reported
// as "don't see a start/stop item, or a status item" after signing out
// during testing), and conflates two genuinely separate concerns: whether
// the relay tunnel is running, and whether anyone's currently paired to
// this installation. setEnabled/ensureTunnel/stopTunnel never required
// credentials in the first place (only the presence-reporting call inside
// them is separately gated on having a device_token) -- this was previously
// just a visibility choice on top of an already-independent capability, not
// a real dependency. mToggle and mStatus are both always visible now (set
// once at creation in onReady, never Hidden here); "Include in startup items"
// (mStartup in main.go) was already independent of sign-in state and isn't
// touched here either.
//
// The tooltip additionally notes the number of paired accounts once more
// than one is signed in -- a small, low-risk nod to multi-account pairing;
// per-account entries/sign-out buttons inside the menu itself are
// explicitly deferred (systray's static-menu-item model makes dynamic
// add/remove nontrivial, and each account can already be signed out
// independently from the website itself, which only ever knows its own
// command_key).
//
// The status dot itself is set via setStatusDotIcon (statusicon_darwin.go),
// not MenuItem.SetIcon -- SetIcon sets NSMenuItem.image, a separate column
// that pushes this item's title text right of every other item's; the
// state/checkmark column setStatusDotIcon uses instead is the one "Run on
// system startup"'s checkbox already occupies, so the dot lines up with
// that checkmark and this item's title stays flush with the rest (reported
// directly as misaligned before this). SetTitle first, then
// setStatusDotIcon -- the latter finds the item by matching its current
// title's prefix, so the rename has to land first.
func (d *daemonState) applyState() {
	if d.mToggle == nil || d.mStatus == nil {
		return
	}
	suffix := ""
	if n := d.identityCount(); n > 1 {
		suffix = fmt.Sprintf(" (%d accounts)", n)
	}
	if d.isEnabled() {
		d.mToggle.SetTitle("Pause")
		d.mStatus.SetTitle("Service is running")
		setStatusDotIcon("Service is", greenDotIcon, dotCanvasWidthPt, dotCanvasHeightPt)
		systray.SetTooltip("Casper — connected" + suffix)
	} else {
		d.mToggle.SetTitle("Resume")
		d.mStatus.SetTitle("Service is paused")
		setStatusDotIcon("Service is", grayDotIcon, dotCanvasWidthPt, dotCanvasHeightPt)
		systray.SetTooltip("Casper — paused" + suffix)
	}
}
