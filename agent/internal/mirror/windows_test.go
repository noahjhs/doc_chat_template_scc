package mirror

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestIgnoreLineAnchorsAndEscapes(t *testing.T) {
	for in, want := range map[string]string{
		"a.txt":            "/a.txt",
		"sub/My [1] #2.md": `/sub/My \[1\] \#2.md`,
		"x*y":              `/x\*y`,
	} {
		if got := ignoreLineFor(in, false); got != want {
			t.Errorf("ignoreLineFor(%q) = %q, want %q", in, got, want)
		}
	}
	if got := ignoreLineFor("sub/My [1] {a} #2.md", true); got != "/sub/My [[]1? [{]a[}] #2.md" {
		t.Errorf("windows form: %q", got)
	}
}

func TestSystemNoise(t *testing.T) {
	for _, n := range []string{".DS_Store", "Thumbs.db", "THUMBS.DB", "desktop.ini", "~$Budget.xlsx"} {
		if !isSystemNoise(n) {
			t.Errorf("%q should be noise", n)
		}
	}
	for _, n := range []string{"Budget.xlsx", "thumbs.png", "a~$b"} {
		if isSystemNoise(n) {
			t.Errorf("%q is a real file", n)
		}
	}
}

func TestWithinIsCaseInsensitiveOnWindowsOnly(t *testing.T) {
	root := filepath.Join(string(filepath.Separator), "Users", "Sam")
	in := filepath.Join(string(filepath.Separator), "users", "sam", "Docs")
	if got := Within(root, in); got != (runtime.GOOS == "windows") {
		t.Fatalf("Within case-folding = %v on %s", got, runtime.GOOS)
	}
	if Within(root, filepath.Join(root, "..", "Other")) {
		t.Fatal("a sibling is not within")
	}
}

func TestMoveIntoReplacesAnExistingFile(t *testing.T) {
	dir := t.TempDir()
	src, dst := filepath.Join(dir, "src"), filepath.Join(dir, "sub", "dst")
	os.WriteFile(src, []byte("new"), 0o644)
	os.MkdirAll(filepath.Dir(dst), 0o755)
	os.WriteFile(dst, []byte("old"), 0o644)
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

func TestScanCloudOnlyFindsNothingInOrdinaryFolders(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "a.txt"), []byte("x"), 0o644)
	if got := scanCloudOnly(dir, func(string) bool { return false }); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

func TestCloudOnlyFilesAreIgnoredAndReported(t *testing.T) {
	bin := syncthingBin(t)
	owner, mirror := startNode(t, bin), startNode(t, bin)
	owner.m.cloudCheck = func(p string) bool { return strings.HasPrefix(filepath.Base(p), "cloud-") }
	const fid = "casper-test-onedrive"
	src := t.TempDir()
	os.MkdirAll(filepath.Join(src, "sub"), 0o755)
	os.WriteFile(filepath.Join(src, "here.txt"), []byte("on this PC"), 0o644)
	os.WriteFile(filepath.Join(src, "sub", "cloud-[1] #2.docx"), []byte("placeholder"), 0o644)
	f := OwnedFolder{ID: fid, Label: "Documents", Path: src, Mirrors: []string{mirror.m.DeviceID()}, Password: "a long folder password for tests"}
	d := Desired{Peers: []Peer{mirror.peer("mirror")}, Owned: []OwnedFolder{f}}
	mustApply(t, owner, d)
	mustApply(t, mirror, Desired{Peers: []Peer{owner.peer("owner")}, Held: []HeldFolder{{ID: fid, Label: "riley", Owners: []string{owner.m.DeviceID()}, Kind: "mirror"}}})
	waitFor(t, "here.txt copied, the placeholder reported", 60*time.Second, func() bool {
		st, _ := owner.m.OwnedStatus(f)
		return st.Error == "" && st.UnreadableItems == 1 && len(st.Peers) == 1 && st.Peers[0].Completion == 100 &&
			DirSize(mirror.m.HeldPath(fid)) > 0
	})
	st, _ := owner.m.OwnedStatus(f)
	if st.UnreadableNames[0] != "sub/cloud-[1] #2.docx" {
		t.Fatalf("unreadable = %v", st.UnreadableNames)
	}
	var gs struct {
		GlobalFiles int `json:"globalFiles"`
	}
	waitFor(t, "here.txt indexed", 30*time.Second, func() bool {
		return owner.m.get("/rest/db/status?folder="+fid, &gs) == nil && gs.GlobalFiles >= 1
	})
	if gs.GlobalFiles != 1 {
		t.Fatalf("the placeholder was indexed (it would be downloaded): %+v", gs)
	}
}
