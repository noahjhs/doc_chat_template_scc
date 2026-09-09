package commands

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
	"time"
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
	h := New(nil, nil)
	h.AddRoot(resolvedRoot)
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

func TestNoRootsMeansEveryCommandFails(t *testing.T) {
	h := New(nil, nil)
	res, err := h.Dispatch(&Request{Action: "pwd"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Success {
		t.Fatal("expected pwd to fail with zero roots")
	}
	if _, err := h.Dispatch(&Request{Action: "ls", Path: "."}); err == nil {
		t.Fatal("expected ls to fail (as an ActionError from resolvePath) with zero roots")
	}
}

func TestMultipleRootsAreEachConfinedAndIndependentlyAddressable(t *testing.T) {
	h := New(nil, nil)
	rootA, err := realpath(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	rootB, err := realpath(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h.AddRoot(rootA)
	h.AddRoot(rootB)

	if got := h.Roots(); len(got) != 2 || got[0] != rootA || got[1] != rootB {
		t.Fatalf("expected [%s %s], got %v", rootA, rootB, got)
	}

	// AddRoot switches cwd to whatever was just added (rootB, most recently).
	if res, err := h.Dispatch(&Request{Action: "pwd"}); err != nil || res.Stdout != rootB {
		t.Fatalf("expected cwd to be rootB (%s), got %q (err=%v)", rootB, res.Stdout, err)
	}

	// A command can still reach rootA by absolute path even while cwd is in rootB.
	if _, err := os.Create(filepath.Join(rootA, "a.txt")); err != nil {
		t.Fatal(err)
	}
	if res, err := h.Dispatch(&Request{Action: "cat", Path: filepath.Join(rootA, "a.txt")}); err != nil || !res.Success {
		t.Fatalf("expected cat of a file in rootA to succeed while cwd is in rootB: res=%+v err=%v", res, err)
	}

	// But a path outside both roots is still rejected.
	if _, err := h.Dispatch(&Request{Action: "cd", Path: t.TempDir()}); err == nil {
		t.Fatal("expected cd into an unrelated directory to be rejected")
	}
}

func TestAddRootIsIdempotent(t *testing.T) {
	h := New(nil, nil)
	root, err := realpath(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h.AddRoot(root)
	h.AddRoot(root)
	if got := h.Roots(); len(got) != 1 {
		t.Fatalf("expected AddRoot to be idempotent, got %v", got)
	}
}

func TestListDirectories(t *testing.T) {
	h := New(nil, nil)
	if res, err := h.Dispatch(&Request{Action: "list_directories"}); err != nil || res.Stdout != "No directories added yet." {
		t.Fatalf("expected the empty-roots message, got %q (err=%v)", res.Stdout, err)
	}
	root, err := realpath(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h.AddRoot(root)
	res, err := h.Dispatch(&Request{Action: "list_directories"})
	if err != nil || res.Stdout != root {
		t.Fatalf("expected %q, got %q (err=%v)", root, res.Stdout, err)
	}
}

func TestAddDirectoryWithoutPickerConfigured(t *testing.T) {
	h := New(nil, nil)
	_, err := h.Dispatch(&Request{Action: "add_directory"})
	if err == nil {
		t.Fatal("expected add_directory to fail cleanly when no picker was injected")
	}
	if _, ok := err.(*ActionError); !ok {
		t.Fatalf("expected an ActionError, got: %v", err)
	}
}

func TestAddDirectoryViaInjectedPicker(t *testing.T) {
	root, err := realpath(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	added := make(chan string, 1)
	h := New(
		func() (string, error) { return root, nil },
		func(dir string) { added <- dir },
	)
	res, err := h.Dispatch(&Request{Action: "add_directory"})
	if err != nil || !res.Success {
		t.Fatalf("expected add_directory to report success immediately: res=%+v err=%v", res, err)
	}
	select {
	case got := <-added:
		if got != root {
			t.Fatalf("onRootAdded called with %q, want %q", got, root)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("onRootAdded was never called")
	}
	if got := h.Roots(); len(got) != 1 || got[0] != root {
		t.Fatalf("expected the picked directory to be added, got %v", got)
	}
}
