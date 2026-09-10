//go:build !darwin

package commands

import "os/exec"

// No Seatbelt equivalent wired up on this platform yet -- Landlock+seccomp
// on Linux, Job Objects/AppContainer on Windows would be the analogous
// follow-up (see the "Resources: command templates" plan's Build vs. buy
// section). Falls back to a bare exec.Command; the argv-allowlist check
// that already happened by the time this is called (templates.go) remains
// the real structural boundary on this platform.
func sandboxedCommand(binary string, args []string, roots []string) (*exec.Cmd, func()) {
	return exec.Command(binary, args...), func() {}
}
