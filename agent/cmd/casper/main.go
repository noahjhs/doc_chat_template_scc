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
	// Registered before anything else -- in particular, before
	// config.ResolveWorkspaceDir() below, which can block on a native
	// folder-picker dialog, and before systray.Run(). A throwaway spike
	// confirmed registering this late (even just inside systray's onReady)
	// reliably misses the Apple Event on a cold launch triggered by the
	// pairing link itself; registering here catches both that case and the
	// already-running case. Events that arrive before setup below finishes
	// just queue here rather than being dropped.
	pendingPairURLs := make(chan string, 4)
	if err := urlscheme.Register(func(rawURL string) {
		select {
		case pendingPairURLs <- rawURL:
		default: // buffer full -- extremely unlikely; drop rather than block the dispatch thread
		}
	}); err != nil {
		fmt.Fprintf(os.Stderr, "Couldn't register the casper:// URL handler: %s\n", err)
	}

	workspaceDir, err := config.ResolveWorkspaceDir()
	if err != nil {
		fatal("Couldn't resolve a workspace directory: %s", err)
	}

	logPath := os.Getenv("CONTROL_TOOL_LOG_FILE")
	if logPath == "" {
		logPath = filepath.Join(workspaceDir, "command_log.txt")
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

	cmdHandler := commands.New(workspaceDir)
	srv := server.New("", cmdHandler, logger)
	srv.ClearSession = func() { config.ClearSession(logf) }

	state := newDaemonState(srv, relayDomain, authDomain, routingKey, port, workspaceDir, logf)
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

	mToggle := systray.AddMenuItem("Turn off", "Pause/resume the relay connection")
	mSignOut := systray.AddMenuItem("Sign out", "Sign out of Casper")
	systray.AddSeparator()
	mQuit := systray.AddMenuItem("Quit", "Quit Casper")

	state.mToggle = mToggle
	state.mSignOut = mSignOut
	state.applyState() // covers a pairing event that arrived before this ran

	go func() {
		for range mToggle.ClickedCh {
			state.setEnabled(!state.isEnabled())
		}
	}()
	go func() {
		for range mSignOut.ClickedCh {
			logf("Sign out requested from the status bar")
			state.signOutFromTray()
		}
	}()
	go func() {
		<-mQuit.ClickedCh
		logf("Quit requested from the status bar")
		systray.Quit()
	}()
}

func onExit(state *daemonState, srv *server.Server, logf func(format string, args ...any)) {
	state.stopTunnel()
	srv.Shutdown()
	logf("casper-agent exiting")
}
