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
