//go:build darwin

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"casper-agent/internal/dialog"
)

// moveToApplications handles the first launch of a freshly downloaded app.
// Run straight from Downloads, macOS starts it from a temporary, random,
// read-only "App Translocation" copy: "start at login" would point at a path
// that vanishes at the next restart, and every other tool that expects Casper
// in Applications would miss it. So, once, ask to move it there -- and if
// they agree, copy it, start the copy, and quit this one. Returns only if
// nothing was moved.
func moveToApplications(logf func(format string, args ...any)) {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	const marker = ".app/Contents/MacOS/"
	idx := strings.Index(exe, marker)
	if idx == -1 {
		return // a bare dev build, not a bundle
	}
	app := exe[:idx+len(".app")]
	home, _ := os.UserHomeDir()
	if !strings.Contains(app, "/AppTranslocation/") && !strings.HasPrefix(app, filepath.Join(home, "Downloads")+"/") {
		return // already somewhere permanent
	}
	if yes, answered := dialog.Confirm("Move Casper to your Applications folder? It keeps Casper available after a restart.",
		"Move", "Not now", 5*time.Minute); !answered || !yes {
		return
	}
	dest := filepath.Join("/Applications", filepath.Base(app))
	if f, err := os.Stat("/Applications"); err != nil || f.Mode().Perm()&0o200 == 0 {
		dest = filepath.Join(home, "Applications", filepath.Base(app))
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		logf("Couldn't prepare %s: %s", filepath.Dir(dest), err)
		return
	}
	_ = os.RemoveAll(dest)
	if out, err := exec.Command("ditto", app, dest).CombinedOutput(); err != nil {
		logf("Couldn't copy Casper to %s: %s %s", dest, err, strings.TrimSpace(string(out)))
		dialog.ShowError("Couldn't move Casper to " + filepath.Dir(dest) + ". It will keep working from here.")
		return
	}
	if err := exec.Command("open", "-n", dest).Start(); err != nil {
		logf("Couldn't start the moved copy: %s", err)
		return
	}
	logf("Moved Casper to %s; exiting so the copy takes over", dest)
	os.Exit(0)
}
