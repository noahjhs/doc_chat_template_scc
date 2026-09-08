// Casper — your friendly ghost. Run this on your own machine; first run
// opens a browser tab to sign up or log in, after that it's silent. For a
// non-interactive run (dev/CI), set CONTROL_TOOL_KEY directly.
//
// A port of casper_tool.py's __main__ orchestration, tying together the
// config/browser/dialog/pairing/tunnel/commands/server packages.
package main

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/pairing"
	"casper-agent/internal/server"
	"casper-agent/internal/tunnel"
)

func usage() {
	fmt.Fprint(os.Stderr, `Casper — your friendly ghost

Usage: casper [--agent-server [HOST[:PORT]]] [--browser NAME]

  --agent-server [HOST[:PORT]]
        Open the chat app at HOST[:PORT] (default localhost:8501, Streamlit's
        default) instead of the baked-in domain -- for testing against a
        Streamlit instance running elsewhere.

  --browser NAME
        macOS application name of the browser to use, e.g. 'Safari',
        'Google Chrome', 'Firefox' -- for testing against a browser other
        than your system default. No effect on non-macOS.
`)
}

// parseArgs replicates argparse's nargs="?" behavior for --agent-server
// (bare flag -> "localhost:8501"; flag with a value -> that value; flag
// absent -> nil) -- Go's stdlib flag package has no equivalent mode, so this
// is done by hand rather than fighting it into that shape.
func parseArgs(args []string) (agentServer *string, browserName string) {
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "-h" || arg == "--help":
			usage()
			os.Exit(0)
		case arg == "--agent-server":
			if i+1 < len(args) && !strings.HasPrefix(args[i+1], "-") {
				v := args[i+1]
				agentServer = &v
				i++
			} else {
				v := "localhost:8501"
				agentServer = &v
			}
		case strings.HasPrefix(arg, "--agent-server="):
			v := strings.TrimPrefix(arg, "--agent-server=")
			agentServer = &v
		case arg == "--browser":
			if i+1 < len(args) {
				browserName = args[i+1]
				i++
			}
		case strings.HasPrefix(arg, "--browser="):
			browserName = strings.TrimPrefix(arg, "--browser=")
		}
	}
	return
}

// fatal shows a native error dialog and exits -- the only way a startup
// failure is ever visible to the user now that the binary runs directly
// with no console (see build/build_go_macos.sh). Still prints too, which
// remains useful when run from an actual terminal during development.
func fatal(format string, args ...any) {
	message := fmt.Sprintf(format, args...)
	fmt.Fprintln(os.Stderr, message)
	dialog.ShowFatalError(message)
	os.Exit(1)
}

func main() {
	agentServerOverride, browserName := parseArgs(os.Args[1:])

	// Resolving the workspace can block on a native folder-picker dialog --
	// deliberately the very first thing that happens, before any logging or
	// network setup, matching casper_tool.py's ROOT_DIR being resolved at
	// module import time (before __main__ runs at all).
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

	appDomain := config.LoadAppDomain()
	if agentServerOverride != nil {
		appDomain = *agentServerOverride
	}
	if appDomain == "" {
		fatal("No web app domain configured (app_server.txt/--agent-server) — can't sign in or open the chat app.")
	}

	// Authenticate before the relay connection starts: pairing only ever
	// talks to localhost (the browser reaches this machine directly, not
	// through the relay), so there's nothing relay-dependent about it.
	authDomain, err := config.LoadAuthDomain()
	if err != nil {
		fatal("%s", err)
	}
	relayDomain, err := config.LoadRelayDomain()
	if err != nil {
		fatal("%s", err)
	}

	var apiKey string
	var tun *tunnel.Tunnel
	if envKey := os.Getenv("CONTROL_TOOL_KEY"); envKey != "" {
		apiKey = envKey
		tun, _ = pairing.StartAndOpenChat(appDomain, relayDomain, apiKey, port, workspaceDir, true, browserName)
	} else {
		apiKey, tun, err = pairing.EnsureAuthenticated(appDomain, authDomain, relayDomain, port, workspaceDir, browserName, logf)
		if err != nil {
			fatal("%s", err)
		}
	}
	defer tun.Terminate()

	cmdHandler := commands.New(workspaceDir)
	srv := server.New(apiKey, cmdHandler, logger)
	srv.ClearSession = func() { config.ClearSession(logf) }
	srv.OnShutdownRequested = func() {
		dialog.ShowFarewellDialog(logf)
		srv.Shutdown()
	}

	if err := srv.ListenAndServe(fmt.Sprintf("0.0.0.0:%d", port)); err != nil {
		fatal("Server error: %s", err)
	}
}
