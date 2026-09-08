package commands

import (
	"os"
	"path/filepath"
	"testing"
)

func newTestHandler(t *testing.T) (*Handler, string) {
	t.Helper()
	root := t.TempDir()
	// Resolve the root itself through realpath -- on macOS, t.TempDir() lives
	// under /var/folders/..., which is itself a symlink to /private/var/...;
	// RootDir must be in already-resolved form for the confinement check
	// (which compares against already-resolved paths) to work at all.
	resolvedRoot, err := realpath(root)
	if err != nil {
		t.Fatalf("realpath(root): %v", err)
	}
	return New(resolvedRoot), resolvedRoot
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

func TestCdThenRelativeResolution(t *testing.T) {
	h, root := newTestHandler(t)
	sub := filepath.Join(root, "sub")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	res, err := h.Dispatch(&Request{Action: "cd", Path: "sub"})
	if err != nil || !res.Success {
		t.Fatalf("cd failed: res=%+v err=%v", res, err)
	}
	if res.Cwd != sub {
		t.Fatalf("expected cwd %s, got %s", sub, res.Cwd)
	}

	// A subsequent relative resolution should now be relative to "sub", not root.
	if err := os.WriteFile(filepath.Join(sub, "file.txt"), []byte("hi"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err = h.Dispatch(&Request{Action: "cat", Path: "file.txt"})
	if err != nil || !res.Success {
		t.Fatalf("cat failed: res=%+v err=%v", res, err)
	}
	if res.Stdout != "hi" {
		t.Fatalf("expected stdout 'hi', got %q", res.Stdout)
	}
}

func TestMkdirTouchLsRoundTrip(t *testing.T) {
	h, _ := newTestHandler(t)
	req := &Request{Action: "mkdir", Path: "newdir"}
	req.ApplyDefaults()
	if res, err := h.Dispatch(req); err != nil || !res.Success {
		t.Fatalf("mkdir failed: res=%+v err=%v", res, err)
	}

	req = &Request{Action: "touch", Path: "newdir/f.txt"}
	req.ApplyDefaults()
	if res, err := h.Dispatch(req); err != nil || !res.Success {
		t.Fatalf("touch failed: res=%+v err=%v", res, err)
	}

	req = &Request{Action: "ls", Path: "newdir"}
	req.ApplyDefaults()
	res, err := h.Dispatch(req)
	if err != nil || !res.Success {
		t.Fatalf("ls failed: res=%+v err=%v", res, err)
	}
	if res.Stdout == "" {
		t.Fatal("expected ls to list f.txt, got empty output")
	}
}

func TestRmRejectsDirectory(t *testing.T) {
	h, root := newTestHandler(t)
	sub := filepath.Join(root, "adir")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	res, err := h.Dispatch(&Request{Action: "rm", Path: "adir"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected rm on a directory to fail (use rmdir instead)")
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
