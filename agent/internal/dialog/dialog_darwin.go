//go:build darwin

// Package dialog shows the native macOS "farewell" modal right before
// sign-out actually shuts the process down -- a direct port of
// casper_tool.py's show_farewell_dialog()/find_ghost_icon(). ghost.png is
// embedded (staged from assets/ghost.png at the repo root by the build
// script) rather than read from a runtime-extracted PyInstaller bundle dir,
// since Go has no equivalent of sys._MEIPASS.
package dialog

import (
	_ "embed"
	"os"
	"os/exec"
)

//go:embed assets/ghost.png
var ghostPNG []byte

// ShowFarewellDialog is best-effort: a deliberate reminder that Casper (the
// desktop app) is where you come back to start a new session, not the
// browser. Blocks (no timeout -- fine to wait indefinitely for a real "OK"
// click) until dismissed. Any failure here is logged but never blocks the
// shutdown that follows it.
//
// Activates System Events first -- this process has no window/Dock presence
// of its own for "come to the front" to apply to (it's a background server,
// not a GUI app), and without this the dialog can surface behind whatever
// the user was last looking at instead of visibly grabbing attention. This
// is the standard trick for exactly that: activating a real,
// already-focusable application right before "display dialog" carries the
// focus over to it.
func ShowFarewellDialog(logf func(format string, args ...any)) {
	iconClause := ""
	if len(ghostPNG) > 0 {
		tmp, err := os.CreateTemp("", "casper-ghost-*.png")
		if err == nil {
			if _, werr := tmp.Write(ghostPNG); werr == nil {
				iconClause = ` with icon POSIX file "` + tmp.Name() + `"`
			}
			tmp.Close()
			defer os.Remove(tmp.Name())
		}
	}
	script := `tell application "System Events" to activate
display dialog "See you next time!" with title "Casper" buttons {"OK"} default button "OK"` + iconClause
	if out, err := exec.Command("osascript", "-e", script).CombinedOutput(); err != nil {
		if logf != nil {
			logf("show_farewell_dialog: couldn't show it: %s: %s", err, string(out))
		}
	}
}
