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
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"

	"casper-agent/internal/commands"
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

	// No workspace folder chosen (or even choosable) at startup any more --
	// addressable directories are now added on demand, later, via the web
	// app's "+" button (see internal/commands.Handler.AddRoot and its
	// runAddDirectory), so the log file's default location moves to
	// AppConfigDir() (already used for session.json etc.) instead of
	// living inside whatever the workspace happened to be.
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

	// state is assigned below, after cmdHandler -- but onRootAdded (called
	// from inside cmdHandler, possibly from its own background goroutine
	// the moment a folder picker resolves) needs to reach it to push a
	// fresh presence report. Declared first and closed over by reference
	// rather than reordering the two: newDaemonState itself needs srv,
	// which needs cmdHandler, which needs this callback -- a genuine
	// three-way cycle with no dependency-free starting point.
	var state *daemonState
	cmdHandler := commands.New(config.AddWorkspaceDir, func(dir string) {
		logf("Added workspace directory: %s", dir)
		if state != nil {
			state.reportPresenceNow()
		}
	})

	// Directories persist across restarts (workspace.txt, via
	// LoadWorkspaceDirs) -- unless CONTROL_TOOL_WORKSPACE is set, which
	// takes over entirely for a non-interactive run (dev/CI), exactly
	// like the old single-workspace version's env override did: skip the
	// persisted list (and any native dialog) altogether.
	if envDir := os.Getenv("CONTROL_TOOL_WORKSPACE"); envDir != "" {
		if resolved, err := filepath.EvalSymlinks(envDir); err == nil {
			envDir = resolved
		}
		cmdHandler.AddRoot(envDir)
	} else if dirs, err := config.LoadWorkspaceDirs(); err != nil {
		logf("Couldn't load saved workspace directories: %s", err)
	} else {
		for _, dir := range dirs {
			cmdHandler.AddRoot(dir)
		}
	}

	srv := server.New("", cmdHandler, logger)
	srv.ClearSession = func() { config.ClearSession(logf) }

	state = newDaemonState(srv, cmdHandler, relayDomain, authDomain, routingKey, port, logf)
	srv.OnSignOut = state.onSignOut

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
	// literal env value.
	if envKey := os.Getenv("CONTROL_TOOL_KEY"); envKey != "" {
		state.setCredentials(envKey, envKey)
		srv.SetAPIKey(envKey)
		state.setEnabled(true)
	} else {
		// Verifying a cached session can block on a network call --
		// deliberately not on main()'s startup path, so the status-bar icon
		// (and the ability to receive a fresh casper:// pairing) is
		// available immediately even while this is still in flight.
		go func() {
			session, err := config.LoadSession()
			if err != nil || session == nil {
				return
			}
			result := config.VerifyHostSession(authDomain, session.DeviceToken)
			if result == config.VerifyInvalid {
				config.ClearSession(logf)
				logf("Saved session is no longer valid -- waiting to be paired again.")
				return
			}
			if result == config.VerifyUnknown {
				logf("Couldn't verify saved session (offline?) -- using it anyway.")
			}
			logf("Resuming session for %s", session.Username)
			state.resumeSession(session.DeviceToken, session.CommandKey)
		}()
	}

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		logf("received termination signal, shutting down")
		systray.Quit()
	}()

	systray.Run(func() { onReady(state, logf) }, func() { onExit(state, srv, logf) })
}

func onReady(state *daemonState, logf func(format string, args ...any)) {
	systray.SetTitle("👻")
	systray.SetTooltip("Casper")

	// First item, always -- a glance at the icon (green = running, gray =
	// paused) says everything the tooltip already says, without needing to
	// open the menu at all. Disabled: it's a status readout, not an action.
	mStatus := systray.AddMenuItem("Service is paused", "Casper's current status")
	mStatus.Disable()
	systray.AddSeparator()

	mToggle := systray.AddMenuItem("Run", "Pause/resume the relay connection")

	isLoginItem, err := config.IsLoginItem()
	if err != nil {
		logf("Couldn't check login items: %s", err)
	}
	// Independent of sign-in state (unlike mToggle/mStatus above) -- always
	// visible, since "launch Casper at login" is a system-level preference
	// a user might want set before ever pairing a host.
	mStartup := systray.AddMenuItemCheckbox(
		"Include in startup items", "Automatically launch Casper when you log in", isLoginItem,
	)
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("Quit", "Quit Casper")

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

func onExit(state *daemonState, srv *server.Server, logf func(format string, args ...any)) {
	state.stopTunnel()
	srv.Shutdown()
	logf("casper-agent exiting")
}
