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
	"fmt"
	"os"
	"os/exec"
	"strings"
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

// escapeForAppleScript makes an arbitrary string safe to embed inside an
// AppleScript double-quoted string literal (backslashes first, then double
// quotes -- order matters). Skipping this for a message with its own quotes
// or backslashes (e.g. a file path, or a Go error's text) produces malformed
// AppleScript that osascript rejects outright -- confirmed the hard way
// once already by the build script's Terminal-launching wrapper, before
// that wrapper was removed entirely in favor of running the binary directly.
func escapeForAppleScript(s string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`)
	return r.Replace(s)
}

// ShowFatalError displays a native modal for a fatal startup error. Without
// a console (the binary now runs directly, no Terminal-launching wrapper --
// see build/build_go_macos.sh), this is the only way such an error is ever
// visible to the user at all; previously it just printed to a stdout that
// went nowhere. Best-effort: if osascript itself fails, there's no more
// visible fallback left, so this just gives up quietly.
func ShowFatalError(message string) {
	script := fmt.Sprintf(
		`display dialog "%s" with title "Casper" buttons {"OK"} default button "OK" with icon stop`,
		escapeForAppleScript(message),
	)
	_ = exec.Command("osascript", "-e", script).Run()
}

// ShowError displays a native informational modal for a non-fatal error
// that would otherwise be invisible (no console once the binary runs
// directly) -- e.g. a pairing attempt rejected because the host is already
// attached to another account. Unlike ShowFatalError, the caller keeps
// running afterward.
func ShowError(message string) {
	script := fmt.Sprintf(
		`display dialog "%s" with title "Casper" buttons {"OK"} default button "OK" with icon caution`,
		escapeForAppleScript(message),
	)
	_ = exec.Command("osascript", "-e", script).Run()
}

// checkboxUnchecked/checkboxChecked are the two label states of the
// "Don't ask again" toggle in ConfirmWithDontAskAgain below.
const (
	checkboxUnchecked = "☐ Don't ask again"
	checkboxChecked   = "☑ Don't ask again"
)

// ConfirmWithDontAskAgain shows a native modal offering a custom
// affirmative action (actionLabel, the default button), a "Not Now"
// decline, and a "Don't ask again" checkbox -- unchecked by default.
// AppleScript's `display dialog` has no real checkbox widget, so this is
// the standard substitute: the checkbox is itself a third button whose
// label toggles between checkboxUnchecked/checkboxChecked, re-showing the
// same dialog rather than dismissing it, until the user actually picks
// "Not Now" or actionLabel. Returns (accepted, dontAskAgain) -- accepted is
// whether actionLabel was chosen; dontAskAgain reflects the checkbox's
// state at the moment either of those two was clicked. Same
// System-Events-activate trick as ShowFarewellDialog, for the same reason.
func ConfirmWithDontAskAgain(message, actionLabel string) (accepted bool, dontAskAgain bool) {
	script := fmt.Sprintf(`tell application "System Events" to activate
set dontAskChecked to false
set finalButton to ""
repeat
	set checkboxLabel to "%s"
	if dontAskChecked then set checkboxLabel to "%s"
	set dialogResult to display dialog "%s" with title "Casper" buttons {checkboxLabel, "Not Now", "%s"} default button "%s"
	set finalButton to button returned of dialogResult
	if finalButton is "%s" or finalButton is "Not Now" then
		exit repeat
	end if
	set dontAskChecked to not dontAskChecked
end repeat
return finalButton & "|" & dontAskChecked`,
		checkboxUnchecked, checkboxChecked, escapeForAppleScript(message),
		escapeForAppleScript(actionLabel), escapeForAppleScript(actionLabel), escapeForAppleScript(actionLabel),
	)
	out, err := exec.Command("osascript", "-e", script).Output()
	if err != nil {
		return false, false
	}
	parts := strings.SplitN(strings.TrimSpace(string(out)), "|", 2)
	if len(parts) != 2 {
		return false, false
	}
	return parts[0] == actionLabel, parts[1] == "true"
}
