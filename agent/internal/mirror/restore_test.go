package mirror

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func write(t *testing.T, p, body string, mod time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(p, mod, mod); err != nil {
		t.Fatal(err)
	}
}

func TestDatedFileName(t *testing.T) {
	at := time.Date(2026, 10, 14, 9, 12, 0, 0, time.UTC)
	for in, want := range map[string]string{
		"Budget.xlsx": "Budget (as of 2026-10-14 09.12).xlsx",
		"notes":       "notes (as of 2026-10-14 09.12)",
		".env":        ".env (as of 2026-10-14 09.12)",
		"a.tar.gz":    "a.tar (as of 2026-10-14 09.12).gz",
	} {
		if got := DatedFileName(in, at); got != want {
			t.Errorf("DatedFileName(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestFileTarget(t *testing.T) {
	root := t.TempDir()
	at := time.Date(2026, 10, 14, 9, 12, 0, 0, time.UTC)
	got, err := FileTarget(root, "taxes/return.pdf", ModeCopy, "", at)
	if err != nil || got != filepath.Join(root, "taxes", "return (as of 2026-10-14 09.12).pdf") {
		t.Fatalf("copy: %q %v", got, err)
	}
	write(t, got, "x", at)
	if again, _ := FileTarget(root, "taxes/return.pdf", ModeCopy, "", at); again != filepath.Join(root, "taxes", "return (as of 2026-10-14 09.12) 2.pdf") {
		t.Fatalf("a taken name gets a number: %q", again)
	}
	if got, _ := FileTarget(root, "taxes/return.pdf", ModeReplace, "", at); got != filepath.Join(root, "taxes", "return.pdf") {
		t.Fatalf("replace: %q", got)
	}
	if got, _ := FileTarget(root, "taxes/return.pdf", ModeCopy, "/dest", at); got != "/dest/taxes/return.pdf" {
		t.Fatalf("folder copy: %q", got)
	}
	if got, err := FileTarget(root, "../../escape", ModeReplace, "", at); err != nil || got != filepath.Join(root, "escape") {
		t.Fatalf("a path out of the folder stays inside it: %q %v", got, err)
	}
	if _, err := FileTarget(root, "", ModeReplace, "", at); err == nil {
		t.Fatal("an empty path must be refused")
	}
}

func TestFolderCopyRoot(t *testing.T) {
	home := t.TempDir()
	docs := filepath.Join(home, "Documents")
	asOf := time.Date(2026, 10, 10, 18, 0, 0, 0, time.UTC)
	root := FolderCopyRoot(docs, home, asOf)
	if root != filepath.Join(home, "Documents (as of 2026-10-10)") {
		t.Fatalf("got %q", root)
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if got := FolderCopyRoot(docs, home, asOf); got != root+" 2" {
		t.Fatalf("taken: %q", got)
	}
	if got := FolderCopyRoot(home, home, asOf); filepath.Dir(got) != filepath.Join(home, "Casper Restores") {
		t.Fatalf("the home folder itself restores into Casper Restores: %q", got)
	}
}

func TestCopyLocalAsOf(t *testing.T) {
	folder, dest := t.TempDir(), filepath.Join(t.TempDir(), "copy")
	asOf := time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
	before, after := asOf.Add(-48*time.Hour), asOf.Add(48*time.Hour)
	write(t, filepath.Join(folder, "old.txt"), "old", before)
	write(t, filepath.Join(folder, "sub", "deep.txt"), "deep", before)
	write(t, filepath.Join(folder, "new.txt"), "new", after)
	write(t, filepath.Join(folder, "changed.txt"), "now", after)
	write(t, filepath.Join(folder, ".stversions", "x"), "history", before)
	write(t, filepath.Join(folder, ".DS_Store"), "junk", before)
	write(t, filepath.Join(folder, "old (as of 2026-10-01 09.12).txt"), "an earlier restore", before) // made later, keeps its old time
	write(t, filepath.Join(folder, "old (as of 2026-10-01 09.12) 2.txt"), "another", before)
	write(t, filepath.Join(folder, "Photos (as of 2026-10-01)", "a.jpg"), "a restored folder", before)
	skip := map[string]bool{"changed.txt": true}
	if n := LocalBytesAsOf(folder, asOf, skip); n != int64(len("old")+len("deep")) {
		t.Fatalf("bytes = %d", n)
	}
	files, n, err := CopyLocalAsOf(folder, dest, asOf, skip)
	if err != nil || files != 2 || n != int64(len("old")+len("deep")) {
		t.Fatalf("copied %d files, %d bytes, %v", files, n, err)
	}
	for _, rel := range []string{"old.txt", "sub/deep.txt"} {
		if _, err := os.Stat(filepath.Join(dest, rel)); err != nil {
			t.Errorf("%s should have been copied", rel)
		}
	}
	for _, rel := range []string{"new.txt", "changed.txt", ".stversions", ".DS_Store", "old (as of 2026-10-01 09.12).txt", "old (as of 2026-10-01 09.12) 2.txt", "Photos (as of 2026-10-01)"} {
		if _, err := os.Stat(filepath.Join(dest, rel)); err == nil {
			t.Errorf("%s should not have been copied", rel)
		}
	}
	if st, _ := os.Stat(filepath.Join(dest, "old.txt")); !st.ModTime().Equal(before) {
		t.Errorf("a copy keeps its time: %v", st.ModTime())
	}
}

func TestMoveIntoReplacesAtomically(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "src"), filepath.Join(dir, "a", "b", "dst")
	write(t, src, "new", time.Now())
	write(t, dst, "old", time.Now())
	if err := MoveInto(src, dst); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(dst); string(b) != "new" {
		t.Fatalf("dst = %q", b)
	}
	if _, err := os.Stat(src); err == nil {
		t.Fatal("src should be gone")
	}
}

func TestFreeBytes(t *testing.T) {
	if FreeBytes(filepath.Join(t.TempDir(), "not", "there", "yet")) <= 0 {
		t.Fatal("free space of a missing path is its nearest parent's")
	}
}
