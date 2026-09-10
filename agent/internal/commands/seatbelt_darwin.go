//go:build darwin

package commands

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// buildSandboxProfile generates a macOS Seatbelt (SBPL) profile confined to
// roots -- confirmed working end-to-end against real invocations (git
// status/log, python3, including a file write inside a confined root
// succeeding and one outside it, plus outbound network, both being
// blocked): (deny default) covers network on its own (no (allow network*)
// rule at all), file-write* is confined to roots, and file-read* /
// process-fork / process-exec* stay broad -- a real build tool needs to
// read arbitrary system/dependency paths, and restricting that tightly
// breaks legitimate use without materially improving safety (the
// argv-allowlist check in templates.go, not this, is what constrains
// *which* command runs at all; this is defense-in-depth on top of that).
// The /dev/null etc. rules exist because without them even `git status`
// fails outright ("could not open '/dev/null' for reading and writing") --
// discovered directly, not assumed.
func buildSandboxProfile(roots []string) string {
	var b strings.Builder
	b.WriteString("(version 1)\n(deny default)\n(allow process-fork)\n(allow process-exec*)\n(allow file-read*)\n")
	b.WriteString(`(allow file-write-data (literal "/dev/null") (literal "/dev/stdin") (literal "/dev/stdout") (literal "/dev/stderr"))` + "\n")
	b.WriteString(`(allow file-ioctl (literal "/dev/null") (literal "/dev/stdin") (literal "/dev/stdout") (literal "/dev/stderr"))` + "\n")
	b.WriteString("(allow mach-lookup)\n(allow sysctl-read)\n(allow iokit-open)\n")
	if len(roots) > 0 {
		b.WriteString("(allow file-write*\n")
		for _, r := range roots {
			fmt.Fprintf(&b, "  (subpath %q)\n", r)
		}
		b.WriteString(")\n")
	}
	return b.String()
}

// sandboxedCommand wraps exec.Command(binary, args...) so it actually runs
// under the Seatbelt profile above, confined to roots. Falls back to a
// bare exec.Command if the profile file can't be written -- best-effort,
// matching this codebase's posture elsewhere (a missing sandbox layer
// shouldn't itself take down an otherwise-valid, already argv-validated
// command). Returns a cleanup func the caller must defer *after* running
// the command -- the profile is a temp file only needed for this one
// invocation's lifetime, and this function doesn't run the command itself
// (the caller sets Stdout/Stderr/Dir first), so it can't safely clean up
// on its own.
func sandboxedCommand(binary string, args []string, roots []string) (*exec.Cmd, func()) {
	f, err := os.CreateTemp("", "casper-sandbox-*.sb")
	if err != nil {
		return exec.Command(binary, args...), func() {}
	}
	cleanup := func() { os.Remove(f.Name()) }
	if _, err := f.WriteString(buildSandboxProfile(roots)); err != nil {
		f.Close()
		cleanup()
		return exec.Command(binary, args...), func() {}
	}
	if err := f.Close(); err != nil {
		cleanup()
		return exec.Command(binary, args...), func() {}
	}
	fullArgs := append([]string{"-f", f.Name(), binary}, args...)
	return exec.Command("sandbox-exec", fullArgs...), cleanup
}
