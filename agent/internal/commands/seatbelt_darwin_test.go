//go:build darwin

package commands

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestSandboxedCommand_WriteConfinedToRoot is a real, end-to-end enforcement
// test -- runs an actual `sandbox-exec`-wrapped process (not a check of the
// generated profile string) and confirms the confinement matches
// buildSandboxProfile's own stated design (see its doc comment): a write
// succeeds inside a confined root and is denied outside it, regardless of
// what the daemon's own argv-allowlist/policy layer would have allowed --
// this is the defense-in-depth layer underneath that. Until this test
// existed, only a one-time manual check (referenced in buildSandboxProfile's
// own comment) had ever confirmed this actually works.
//
// t.TempDir() paths are resolved via filepath.EvalSymlinks before use here,
// mirroring cmd/casper/main.go's own real startup handling of homeRoot --
// macOS's /var (and so /var/folders, where TempDir lives) is itself a
// symlink to /private/var, and Seatbelt's (subpath ...) matching needs the
// resolved form to behave the same way it does against the daemon's real,
// already-resolved homeRoot.
func TestSandboxedCommand_WriteConfinedToRoot(t *testing.T) {
	if _, err := exec.LookPath("sandbox-exec"); err != nil {
		t.Skip("sandbox-exec not available on this machine")
	}
	root := resolvedTempDir(t)
	outside := resolvedTempDir(t)

	insidePath := filepath.Join(root, "inside.txt")
	cmd, cleanup := sandboxedCommand("/usr/bin/touch", []string{insidePath}, []string{root})
	defer cleanup()
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("write inside the confined root should have succeeded: %v\n%s", err, out)
	}
	if _, err := os.Stat(insidePath); err != nil {
		t.Fatalf("expected %s to exist after the sandboxed write, got: %v", insidePath, err)
	}

	outsidePath := filepath.Join(outside, "outside.txt")
	cmd2, cleanup2 := sandboxedCommand("/usr/bin/touch", []string{outsidePath}, []string{root})
	defer cleanup2()
	if err := cmd2.Run(); err == nil {
		t.Fatal("write outside the confined root should have been denied by the sandbox, but touch succeeded")
	}
	if _, err := os.Stat(outsidePath); err == nil {
		t.Fatalf("expected %s to NOT exist -- the sandbox should have blocked the write", outsidePath)
	}
}

// TestSandboxedCommand_ReadOutsideRootStillAllowed pins down the other half
// of buildSandboxProfile's deliberate design: file-read* is intentionally
// broad (a real build tool needs to read arbitrary system/dependency
// paths), so this must keep succeeding -- if it starts failing, that's a
// real regression in buildSandboxProfile, not a fix.
func TestSandboxedCommand_ReadOutsideRootStillAllowed(t *testing.T) {
	if _, err := exec.LookPath("sandbox-exec"); err != nil {
		t.Skip("sandbox-exec not available on this machine")
	}
	root := resolvedTempDir(t)
	outside := resolvedTempDir(t)
	outsideFile := filepath.Join(outside, "readable.txt")
	if err := os.WriteFile(outsideFile, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}

	cmd, cleanup := sandboxedCommand("/bin/cat", []string{outsideFile}, []string{root})
	defer cleanup()
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf(
			"reading outside the confined root should still be allowed (file-read* is deliberately broad): %v\n%s",
			err, out,
		)
	}
	if string(out) != "hello" {
		t.Fatalf("expected to read back %q, got %q", "hello", out)
	}
}

func resolvedTempDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatalf("couldn't resolve symlinks for temp dir %s: %v", dir, err)
	}
	return resolved
}
