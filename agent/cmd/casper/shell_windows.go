//go:build windows

package main

import (
	"os"
	"path/filepath"
	"sync/atomic"

	"casper-agent/internal/config"
	"casper-agent/internal/console"
	"casper-agent/internal/dialog"
	"casper-agent/internal/instance"
	"casper-agent/internal/urlscheme"

	"github.com/getlantern/systray"
)

var hintOpen atomic.Bool

func instanceName() string { return "Casper-" + config.PairURLScheme() }

// claimInstance makes this the one running Casper, or hands what this process
// was started with (a casper:// link, or just "show me") to the one that is
// and reports false -- Windows starts a new process for every link and every
// double-click. A dev or CI run (CONTROL_TOOL_KEY) never competes.
func claimInstance(deliver func(rawURL string)) bool {
	if os.Getenv("CONTROL_TOOL_KEY") != "" {
		return true
	}
	scheme := config.PairURLScheme()
	first, err := instance.Acquire(instanceName(), func(msg string) {
		if len(urlscheme.FromArgs([]string{msg}, scheme)) == 1 {
			deliver(msg)
		} else {
			showTrayHint(true)
		}
	})
	if err != nil || first {
		return true
	}
	links := urlscheme.FromArgs(os.Args[1:], scheme)
	if len(links) == 0 {
		links = []string{"show"}
	}
	for _, l := range links {
		_ = instance.Send(instanceName(), l)
	}
	return false
}

// releaseInstance lets a restart's new process become the first.
func releaseInstance() { instance.Release() }

func attachConsole() { console.AttachForCLI() }

// applyTrayIcon colours the ghost: green running, grey paused.
func applyTrayIcon(enabled bool) {
	if enabled {
		systray.SetIcon(greenGhostICO)
	} else {
		systray.SetIcon(grayGhostICO)
	}
}

// showTrayHint says where Casper is. Windows tucks a new tray icon into the
// overflow flyout; the first run (and a second launch, force) tells the person.
func showTrayHint(force bool) {
	if !hintOpen.CompareAndSwap(false, true) { // one at a time, however many launches
		return
	}
	defer hintOpen.Store(false)
	dir, err := config.AppConfigDir()
	if err != nil {
		return
	}
	flag := filepath.Join(dir, "tray_hint_shown.txt")
	if !force {
		if _, err := os.Stat(flag); err == nil {
			return
		}
		_ = os.WriteFile(flag, []byte("shown\n"), 0o644)
	}
	dialog.Info("Casper is running. Its ghost is in the corner of the taskbar, next to the clock; if you don't see it, click the little arrow (^) there. Drag the ghost onto the taskbar to keep it in view.")
}
