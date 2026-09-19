package commands

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func newTestHandler(t *testing.T) (*Handler, string) {
	t.Helper()
	root := t.TempDir()
	// Resolve the root itself through realpath -- on macOS, t.TempDir() lives
	// under /var/folders/..., which is itself a symlink to /private/var/...;
	// a root must be in already-resolved form for the confinement check
	// (which compares against already-resolved paths) to work at all.
	resolvedRoot, err := realpath(root)
	if err != nil {
		t.Fatalf("realpath(root): %v", err)
	}
	h := New(resolvedRoot)
	return h, resolvedRoot
}

func TestResolvePath_RejectsDotDotEscape(t *testing.T) {
	h, root := newTestHandler(t)
	_ = root
	_, err := h.resolvePath("../../etc/passwd", false, false)
	if err == nil {
		t.Fatal("expected an error escaping the root via .., got none")
	}
	if _, ok := err.(*ActionError); !ok {
		t.Fatalf("expected *ActionError, got %T: %v", err, err)
	}
}

func TestResolvePath_RejectsSymlinkEscape(t *testing.T) {
	h, root := newTestHandler(t)
	outside := t.TempDir() // a different temp dir, outside root
	secretFile := filepath.Join(outside, "secret.txt")
	if err := os.WriteFile(secretFile, []byte("top secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "escape")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}

	_, err := h.resolvePath("escape/secret.txt", true, false)
	if err == nil {
		t.Fatal("expected a symlink escape to be rejected, got none")
	}
}

func TestResolvePath_AllowsNonExistentTargetInsideRoot(t *testing.T) {
	h, _ := newTestHandler(t)
	// mkdir/touch/cp-destination targets don't exist yet -- resolvePath must
	// still succeed (resolving the existing parent, appending the rest).
	resolved, err := h.resolvePath("newdir/newfile.txt", false, false)
	if err != nil {
		t.Fatalf("expected non-existent-but-inside-root path to resolve, got: %v", err)
	}
	if filepath.Base(resolved) != "newfile.txt" {
		t.Fatalf("resolved path looks wrong: %s", resolved)
	}
}

func TestResolvePath_AbsolutePathOutsideRootRejected(t *testing.T) {
	h, _ := newTestHandler(t)
	_, err := h.resolvePath("/etc/passwd", false, false)
	if err == nil {
		t.Fatal("expected an absolute path outside root to be rejected")
	}
}

func TestDispatch_UnknownAction(t *testing.T) {
	h, _ := newTestHandler(t)
	_, err := h.Dispatch(&Request{Action: "rm -rf"})
	if err == nil {
		t.Fatal("expected an unknown action to be rejected")
	}
	if ae, ok := err.(*ActionError); !ok || ae.Detail != "Action not authorized." {
		t.Fatalf("expected 'Action not authorized.', got: %v", err)
	}
}

func TestReadWriteFileRoundTrip(t *testing.T) {
	h, root := newTestHandler(t)
	// Includes a null byte -- confirms this is a real binary-safe
	// round-trip, not just a text one that happens to work.
	original := []byte{0x00, 0x01, 0xff, 'h', 'i'}
	content := base64.StdEncoding.EncodeToString(original)

	written, err := h.Dispatch(&Request{Action: "write_file", Path: "bin.dat", Content: content})
	if err != nil || !written.Success {
		t.Fatalf("write_file failed: result=%+v err=%v", written, err)
	}
	if _, statErr := os.Stat(filepath.Join(root, "bin.dat")); statErr != nil {
		t.Fatalf("expected bin.dat to exist: %v", statErr)
	}

	read, err := h.Dispatch(&Request{Action: "read_file", Path: "bin.dat"})
	if err != nil || !read.Success {
		t.Fatalf("read_file failed: result=%+v err=%v", read, err)
	}
	decoded, err := base64.StdEncoding.DecodeString(read.Stdout)
	if err != nil {
		t.Fatalf("read_file returned invalid base64: %v", err)
	}
	if string(decoded) != string(original) {
		t.Fatalf("round-trip mismatch: got %v, want %v", decoded, original)
	}
}

func TestWriteFileRejectsOversizedContent(t *testing.T) {
	h, _ := newTestHandler(t)
	oversized := base64.StdEncoding.EncodeToString(make([]byte, maxTransferFileBytes+1))
	_, err := h.Dispatch(&Request{Action: "write_file", Path: "huge.bin", Content: oversized})
	if err == nil {
		t.Fatal("expected an oversized write_file to be rejected")
	}
}

func TestReadFileRejectsDirectory(t *testing.T) {
	h, root := newTestHandler(t)
	if err := os.Mkdir(filepath.Join(root, "adir"), 0o755); err != nil {
		t.Fatal(err)
	}
	res, err := h.Dispatch(&Request{Action: "read_file", Path: "adir"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected read_file on a directory to fail")
	}
}

func TestWriteFileRejectsEscapingRoot(t *testing.T) {
	h, _ := newTestHandler(t)
	_, err := h.Dispatch(&Request{Action: "write_file", Path: "../outside.txt", Content: base64.StdEncoding.EncodeToString([]byte("x"))})
	if err == nil {
		t.Fatal("expected write_file to reject a path escaping RootDir")
	}
	if _, ok := err.(*ActionError); !ok {
		t.Fatalf("expected an ActionError, got: %v", err)
	}
}

func TestNoHomeRootMeansEveryCommandFails(t *testing.T) {
	h := New("")
	if _, err := h.Dispatch(&Request{Action: "read_file", Path: "whatever.txt"}); err == nil {
		t.Fatal("expected read_file to fail (as an ActionError from resolvePath) with no homeRoot configured")
	}
	h.SetPolicyLayers([]PolicyLayer{{ID: 1, Rules: []Rule{{PositionalConstraints: []Pattern{unconstrained()}, Tier: "allow"}}}})
	res, err := h.Dispatch(&Request{Action: "run_shell_command", PositionalArgs: []string{"echo"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected run_shell_command to fail with no homeRoot configured")
	}
}

func TestHomeRootIsFixedAtConstruction(t *testing.T) {
	h, root := newTestHandler(t)
	if h.HomeRoot() != root {
		t.Fatalf("expected HomeRoot() to report %q, got %q", root, h.HomeRoot())
	}
	// Confirms a path outside homeRoot is rejected -- the confinement
	// boundary is real, not just a reported string.
	if _, err := h.resolvePath(t.TempDir(), true, true); err == nil {
		t.Fatal("expected resolving an unrelated directory to be rejected")
	}
}
