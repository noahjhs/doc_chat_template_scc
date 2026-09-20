// Casper — your friendly ghost. Runs as a persistent background daemon,
// typically added to macOS Login Items and left running indefinitely,
// toggled on/off via its status-bar icon. Users sign in by navigating to
// the deployed web app themselves; a successful sign-in fires a
// casper://pair?token=...&username=... link, which the OS hands to this
// process (already running, or freshly launched) as an Apple Event -- see
// internal/urlscheme and daemon.go's handlePairURL. For a non-interactive
// run (dev/CI), set CONTROL_TOOL_KEY directly.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"

	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/server"
	"casper-agent/internal/urlscheme"

	"github.com/getlantern/systray"
)

// A double-clicked .app bundle never gets these (Finder launches it with no
// arguments) -- this is for a Terminal-launched binary, or `open
// Casper.app --args --clear-preferences`, mainly useful for support/testing
// (resetting a test installation back to its out-of-the-box state without
// hunting down and deleting individual files under AppConfigDir).
var clearPreferences = flag.Bool(
	"clear-preferences", false,
	"Clear saved preferences (currently just the running/paused toggle) and start fresh, defaulting to running.",
)

// fatal shows a native error dialog and exits -- used only for startup
// failures that leave the daemon unable to do anything useful at all (no
// workspace, no log file, no domains configured). Once past startup, a
// failure (relay down, auth service unreachable) no longer takes the whole
// process down -- it just leaves the daemon retrying/idle, since it's now
// meant to keep running indefinitely as a login item.
func fatal(format string, args ...any) {
	message := fmt.Sprintf(format, args...)
	fmt.Fprintln(os.Stderr, message)
	dialog.ShowFatalError(message)
	os.Exit(1)
}

func main() {
	flag.Parse()
	if *clearPreferences {
		config.ClearEnabled()
	}

	// Registered before anything else -- in particular before
	// systray.Run(). A throwaway spike confirmed registering this late
	// (even just inside systray's onReady) reliably misses the Apple Event
	// on a cold launch triggered by the pairing link itself; registering
	// here catches both that case and the already-running case. Events
	// that arrive before setup below finishes just queue here rather than
	// being dropped.
	pendingPairURLs := make(chan string, 4)
	if err := urlscheme.Register(func(rawURL string) {
		select {
		case pendingPairURLs <- rawURL:
		default: // buffer full -- extremely unlikely; drop rather than block the dispatch thread
		}
	}); err != nil {
		fmt.Fprintf(os.Stderr, "Couldn't register the casper:// URL handler: %s\n", err)
	}

	// The log file's default location is AppConfigDir() (already used for
	// session.json etc.), independent of homeRoot below -- a log file
	// living inside the confined directory itself would be a little odd.
	logPath := os.Getenv("CONTROL_TOOL_LOG_FILE")
	if logPath == "" {
		cfgDir, err := config.AppConfigDir()
		if err != nil {
			fatal("Couldn't resolve a config directory: %s", err)
		}
		logPath = filepath.Join(cfgDir, "command_log.txt")
	}
	logFile, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		fatal("Couldn't open log file %s: %s", logPath, err)
	}
	defer logFile.Close()
	logger := log.New(io.MultiWriter(os.Stdout, logFile), "", log.LstdFlags)
	logf := func(format string, args ...any) { logger.Printf(format, args...) }

	port := 8000
	if v := os.Getenv("CONTROL_TOOL_PORT"); v != "" {
		if p, err := strconv.Atoi(v); err == nil {
			port = p
		}
	}

	authDomain, err := config.LoadAuthDomain()
	if err != nil {
		fatal("%s", err)
	}
	relayDomain, err := config.LoadRelayDomain()
	if err != nil {
		fatal("%s", err)
	}
	routingKey, err := config.LoadOrCreateRoutingKey()
	if err != nil {
		fatal("Couldn't set up a relay routing key: %s", err)
	}

	// homeRoot is the single directory every path-taking action is confined
	// to (see commands.Handler's own doc comment) -- the user's home
	// directory by default, computed once here and never mutated
	// afterward. CONTROL_TOOL_WORKSPACE overrides it entirely for a
	// non-interactive run (dev/CI), same env-override posture this had
	// back when it named a dynamic, user-managed workspace list instead of
	// this one fixed directory.
	homeRoot := os.Getenv("CONTROL_TOOL_WORKSPACE")
	if homeRoot == "" {
		dir, err := os.UserHomeDir()
		if err != nil {
			fatal("Couldn't resolve the home directory: %s", err)
		}
		homeRoot = dir
	}
	if resolved, err := filepath.EvalSymlinks(homeRoot); err == nil {
		homeRoot = resolved
	}

	srv := server.New(logger)

	// Long-polls for each paired identity's own pending approvals to answer
	// with a native dialog, for the daemon's whole lifetime -- see
	// runApprovalRelayLoop's own doc comment for why this is a parent
	// context rather than one loop: each identity gets its own child,
	// started/stopped as it's paired/signed out; cancelling this parent (in
	// onExit, mirroring how state.stopTunnel() already tears down
	// tunnel.go's own background loop there) cancels all of them at once.
	approvalRelayCtx, approvalRelayCancel := context.WithCancel(context.Background())

	state := newDaemonState(srv, homeRoot, relayDomain, authDomain, routingKey, port, approvalRelayCtx, logf)

	go func() {
		if err := srv.ListenAndServe(fmt.Sprintf("0.0.0.0:%d", port)); err != nil {
			logf("Server error: %s", err)
		}
	}()

	go func() {
		for rawURL := range pendingPairURLs {
			state.handlePairURL(rawURL)
		}
	}()

	// A non-interactive run (dev/CI): use the key directly, skip the
	// session file/casper:// pairing entirely. There's no separate
	// command_key to distinguish here -- both the device_token (unused,
	// since presence reporting isn't exercised in this mode) and the
	// command_key (what actually gates /api/command) are set to the same
	// literal env value. "dev" is a synthetic username -- this mode never
	// goes through casper://pair, so there's no real one.
	if envKey := os.Getenv("CONTROL_TOOL_KEY"); envKey != "" {
		state.addOrReplaceIdentity("dev", envKey, envKey)
		state.setEnabled(true)
	} else {
		// Verifying cached sessions can block on network calls --
		// deliberately not on main()'s startup path, so the status-bar icon
		// (and the ability to receive a fresh casper:// pairing) is
		// available immediately even while this is still in flight. Each
		// cached session (one per previously-paired account) is verified
		// and resumed independently -- one failing verification doesn't
		// block any other account from resuming.
		go func() {
			sessions, err := config.LoadSessions()
			if err != nil || len(sessions) == 0 {
				return
			}
			for _, session := range sessions {
				result := config.VerifyHostSession(authDomain, session.DeviceToken)
				if result == config.VerifyInvalid {
					state.dropUnresumedSession(session.Username)
					logf("Saved session for %s is no longer valid -- waiting to be paired again.", session.Username)
					continue
				}
				if result == config.VerifyUnknown {
					logf("Couldn't verify saved session for %s (offline?) -- using it anyway.", session.Username)
				}
				logf("Resuming session for %s", session.Username)
				state.resumeIdentity(session)
			}
		}()
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		logf("received termination signal, shutting down")
		systray.Quit()
	}()

	restart := func() { restartApp(state, srv, approvalRelayCancel, logf) }
	systray.Run(func() { onReady(state, restart, logf) }, func() { onExit(state, srv, approvalRelayCancel, logf) })
}

func onReady(state *daemonState, restart func(), logf func(format string, args ...any)) {
	systray.SetTitle("👻")
	systray.SetTooltip("Casper")

	// First item, always -- a glance at the icon (green = running, gray =
	// paused) says everything at a glance, without needing to open the menu
	// at all. Disabled: it's a status readout, not an action. No tooltip
	// strings on any item below (removed per an explicit ask) -- the
	// item's own title is meant to be self-explanatory.
	mStatus := systray.AddMenuItem("Service is paused", "")
	mStatus.Disable()
	systray.AddSeparator()

	mToggle := systray.AddMenuItem("Resume", "")

	isLoginItem, err := config.IsLoginItem()
	if err != nil {
		logf("Couldn't check login items: %s", err)
	}
	// Independent of sign-in state (unlike mToggle/mStatus above) -- always
	// visible, since "launch Casper at login" is a system-level preference
	// a user might want set before ever pairing a host.
	mStartup := systray.AddMenuItemCheckbox("Include in startup items", "", isLoginItem)
	systray.AddSeparator()
	// Restart, not just Quit -- a manual fallback for cases the automatic
	// refresh triggers (pairing/resume, and the Resources page's own
	// on-mutation refresh) don't cover, without having to hunt down and
	// relaunch the app bundle by hand.
	mRestart := systray.AddMenuItem("Restart", "")
	mQuit := systray.AddMenuItem("Quit", "")

	state.mToggle = mToggle
	state.mStatus = mStatus
	state.applyState() // covers a pairing event that arrived before this ran

	go func() {
		for range mToggle.ClickedCh {
			state.setEnabled(!state.isEnabled())
		}
	}()
	go func() {
		for range mStartup.ClickedCh {
			if mStartup.Checked() {
				mStartup.Uncheck()
				if err := config.RemoveLoginItem(); err != nil {
					logf("Couldn't remove Casper from login items: %s", err)
				}
			} else {
				mStartup.Check()
				if err := config.AddLoginItem(); err != nil {
					logf("Couldn't add Casper to login items: %s", err)
					mStartup.Uncheck() // revert the visual state -- the add itself failed
				}
			}
		}
	}()
	go func() {
		<-mRestart.ClickedCh
		restart()
	}()
	go func() {
		<-mQuit.ClickedCh
		logf("Quit requested from the status bar")
		systray.Quit()
	}()

	// Prompted once per cold launch (not gated behind whether the user
	// happened to open the menu), unless it's already a login item, the
	// user previously checked "Don't ask again", or this is a
	// non-interactive dev/CI run (CONTROL_TOOL_KEY set, same env check
	// main() uses to skip pairing dialogs entirely).
	if !isLoginItem && os.Getenv("CONTROL_TOOL_KEY") == "" && !config.LoginItemPromptDismissed() {
		go promptAddToLoginItems(mStartup, logf)
	}
}

// promptAddToLoginItems asks (via a native, declinable modal -- see
// internal/dialog.ConfirmWithDontAskAgain) whether to register Casper as a
// macOS Login Item, so it relaunches automatically after a logout/restart
// -- otherwise "Include in startup items" would only ever get turned on by a
// user who happens to notice the menu item themselves.
func promptAddToLoginItems(mStartup *systray.MenuItem, logf func(format string, args ...any)) {
	accepted, dontAskAgain := dialog.ConfirmWithDontAskAgain(
		"Add Casper to your login items, so it starts automatically when you log in?",
		"Add",
	)
	if dontAskAgain {
		config.DismissLoginItemPrompt()
	}
	if !accepted {
		return
	}
	if err := config.AddLoginItem(); err != nil {
		logf("Couldn't add Casper to login items: %s", err)
		return
	}
	mStartup.Check()
}

// shutdownDaemon is the actual teardown work -- factored out of onExit so
// restartApp can run the exact same steps synchronously itself (see its own
// doc comment for why it can't just go through systray.Quit()/onExit).
func shutdownDaemon(state *daemonState, srv *server.Server, cancelApprovalRelay context.CancelFunc) {
	cancelApprovalRelay()
	state.stopTunnel()
	srv.Shutdown()
}

func onExit(state *daemonState, srv *server.Server, cancelApprovalRelay context.CancelFunc, logf func(format string, args ...any)) {
	shutdownDaemon(state, srv, cancelApprovalRelay)
	logf("casper-agent exiting")
}

// restartApp relaunches the running executable and then exits this process
// -- shuts down first (synchronously, not via systray.Quit()'s own async
// callback) specifically so the new instance never races this one for the
// HTTP server's port; only once that's done does it spawn the replacement
// and exit. The new instance picks its session back up from the same
// cached session.json a normal quit-and-reopen would, so nothing else
// needs to be handed to it explicitly.
func restartApp(state *daemonState, srv *server.Server, cancelApprovalRelay context.CancelFunc, logf func(format string, args ...any)) {
	exePath, err := os.Executable()
	if err != nil {
		logf("Restart: couldn't resolve the running executable's path: %s", err)
		dialog.ShowError("Couldn't restart Casper -- try quitting and reopening it manually.")
		return
	}
	logf("Restart requested from the status bar")
	shutdownDaemon(state, srv, cancelApprovalRelay)
	if err := exec.Command(exePath).Start(); err != nil {
		logf("Restart: couldn't relaunch %s: %s", exePath, err)
		dialog.ShowError("Couldn't restart Casper -- try quitting and reopening it manually.")
		return
	}
	os.Exit(0)
}
