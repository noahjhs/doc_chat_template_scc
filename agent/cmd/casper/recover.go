package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/getlantern/systray"

	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/setup"
)

// restorableFolders is how many of this account's folders live on another
// Mac -- what "Recover from a Lost Mac…" could bring back here.
func (d *daemonState) restorableFolders() (n int, labels []string) {
	for _, id := range d.mirrorIdentities() {
		folders, err := config.ListRestorable(d.authDomain, id.DeviceToken, false)
		if err != nil {
			continue
		}
		for _, f := range folders {
			if !f.Restoring {
				n++
				labels = append(labels, f.Label)
			}
		}
	}
	return
}

// watchRecoverable shows the menu item only while this account has folders
// whose Mac is another one, and offers recovery once, right after pairing.
func watchRecoverable(state *daemonState, mRecover *systray.MenuItem, logf func(format string, args ...any)) {
	dir, _ := config.AppConfigDir()
	offered := filepath.Join(dir, "recovery-offered")
	// Looks often while the item is hidden: the guide tells a newly paired
	// person to choose it as soon as the Mac is connected.
	wait := 5 * time.Second
	for ; ; time.Sleep(wait) {
		n, labels := state.restorableFolders()
		if n == 0 {
			mRecover.Hide()
			wait = 5 * time.Second
			continue
		}
		mRecover.Show()
		wait = time.Minute
		if _, err := os.Stat(offered); err == nil {
			continue
		}
		_ = os.WriteFile(offered, nil, 0o600)
		if yes, answered := dialog.Confirm("Casper keeps "+setup.JoinNames(labels)+" safe for you from another Mac. Is that Mac lost? Bring it back on this Mac with your recovery kit.",
			"Recover", "Not Now", 5*time.Minute); answered && yes {
			recoverFromLostMac(logf)
		}
	}
}

// recoverFromLostMac runs this app's own `setup recover`: the kit file, its
// passphrase and the Restore yes are all asked for in Casper's windows.
func recoverFromLostMac(logf func(format string, args ...any)) {
	exe, err := os.Executable()
	if err != nil {
		dialog.ShowError("Couldn't start recovery: " + err.Error())
		return
	}
	out, _ := exec.Command(exe, "setup", "recover", "--json").Output()
	var result struct {
		OK      bool           `json:"ok"`
		Error   string         `json:"error"`
		Folders map[string]any `json:"folders"`
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &result); err != nil || !result.OK {
		msg := result.Error
		switch {
		case strings.Contains(msg, "cancelled"):
			return
		case strings.Contains(msg, "wrong passphrase"):
			msg = "That passphrase didn't open the kit. Choose Recover from a Lost Mac to try again."
		case strings.Contains(msg, "isn't a Casper recovery kit"):
			msg = "That file isn't a Casper recovery kit."
		case strings.Contains(msg, "doesn't cover"), strings.Contains(msg, "nothing to recover"):
			// already plain words
		case strings.Contains(msg, "is for"):
			// plain words too: wrong account
		default:
			msg = "Couldn't recover your folders. Try again in a minute."
		}
		logf("Recover: %s", result.Error)
		dialog.ShowError(msg)
		return
	}
	logf("Recovering %d folder(s)", len(result.Folders))
	dialog.Confirm("Restoring your folders now. They come back from your friends' computers, and Casper keeps them mirrored afterwards.",
		"OK", "Close", time.Minute)
}
