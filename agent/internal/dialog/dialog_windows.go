//go:build windows

// Package dialog shows native modals. ShowFarewellDialog is a no-op on
// Windows (mirroring casper_tool.py's `if sys.platform != "darwin": return`);
// ShowFatalError has a real Windows implementation since a startup failure
// needs to be visible on every platform, not just macOS.
package dialog

import (
	"fmt"
	"os/exec"
	"strings"
)

func ShowFarewellDialog(_ func(format string, args ...any)) {}

// escapeForPowerShellSingleQuoted doubles up single quotes -- PowerShell's
// escaping rule inside a '...' string literal (its equivalent of the
// AppleScript double-quote-escaping this package's darwin build handles
// separately).
func escapeForPowerShellSingleQuoted(s string) string {
	return strings.ReplaceAll(s, "'", "''")
}

// ShowFatalError displays a native message box for a fatal startup error --
// see the darwin implementation's docs for why this exists at all (no
// console once the binary runs directly, not through a wrapper).
func ShowFatalError(message string) {
	script := fmt.Sprintf(
		"Add-Type -AssemblyName System.Windows.Forms | Out-Null; "+
			"[System.Windows.Forms.MessageBox]::Show('%s', 'Casper', "+
			"[System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)",
		escapeForPowerShellSingleQuoted(message),
	)
	_ = exec.Command("powershell", "-NoProfile", "-Command", script).Run()
}

// ShowError displays a native informational message box for a non-fatal
// error -- see the darwin implementation's docs for why this exists at all.
// Unlike ShowFatalError, the caller keeps running afterward.
func ShowError(message string) {
	script := fmt.Sprintf(
		"Add-Type -AssemblyName System.Windows.Forms | Out-Null; "+
			"[System.Windows.Forms.MessageBox]::Show('%s', 'Casper', "+
			"[System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)",
		escapeForPowerShellSingleQuoted(message),
	)
	_ = exec.Command("powershell", "-NoProfile", "-Command", script).Run()
}

// ConfirmWithDontAskAgain approximates the darwin implementation's
// checkbox-toggle dialog using a three-way MessageBox instead (Windows'
// MessageBox has no real checkbox option either, and there's no packaged
// Windows build yet to warrant a heavier custom dialog for this): Yes ->
// actionLabel accepted, No -> declined, Cancel -> declined and treated as
// "don't ask again". actionLabel itself isn't shown (YesNoCancel's button
// text is fixed), but the caller-visible contract matches the darwin
// version.
func ConfirmWithDontAskAgain(message, _ string) (accepted bool, dontAskAgain bool) {
	script := fmt.Sprintf(
		"Add-Type -AssemblyName System.Windows.Forms | Out-Null; "+
			"$r = [System.Windows.Forms.MessageBox]::Show('%s' + [Environment]::NewLine + "+
			"'(Cancel = don''t ask again)', 'Casper', [System.Windows.Forms.MessageBoxButtons]::YesNoCancel); "+
			"Write-Output $r",
		escapeForPowerShellSingleQuoted(message),
	)
	out, err := exec.Command("powershell", "-NoProfile", "-Command", script).Output()
	if err != nil {
		return false, false
	}
	switch strings.TrimSpace(string(out)) {
	case "Yes":
		return true, false
	case "Cancel":
		return false, true
	default:
		return false, false
	}
}
