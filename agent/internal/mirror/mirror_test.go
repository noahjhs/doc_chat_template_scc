package mirror

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// These tests drive REAL Syncthing processes (owner, mirror, catcher, a
// replacement Mac) on this machine, through Manager only. They need a
// syncthing binary: $CASPER_TEST_SYNCTHING, or ~/st-spike/bin/syncthing.

func syncthingBin(t *testing.T) string {
	t.Helper()
	if p := os.Getenv("CASPER_TEST_SYNCTHING"); p != "" {
		return p
	}
	home, _ := os.UserHomeDir()
	p := filepath.Join(home, "st-spike", "bin", "syncthing")
	if _, err := os.Stat(p); err != nil {
		t.Skip("no syncthing binary (set CASPER_TEST_SYNCTHING)")
	}
	return p
}

func freePort(t *testing.T) int {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

type node struct {
	m    *Manager
	addr string
}

func startNode(t *testing.T, bin string) *node {
	t.Helper()
	dir := t.TempDir()
	port := freePort(t)
	addr := fmt.Sprintf("tcp://127.0.0.1:%d", port)
	m := New(bin, filepath.Join(dir, "st"), filepath.Join(dir, "data"), addr, t.Logf)
	if err := m.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(m.Stop)
	return &node{m: m, addr: addr}
}

func (n *node) peer(name string) Peer {
	return Peer{DeviceID: n.m.DeviceID(), Name: name, Addresses: []string{n.addr}}
}

func waitFor(t *testing.T, what string, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(300 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func mustApply(t *testing.T, n *node, d Desired) {
	t.Helper()
	if err := n.m.Apply(d); err != nil {
		t.Fatal(err)
	}
}

func grepTree(root string, needles ...string) string {
	found := ""
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			if err == nil && info.IsDir() {
				for _, n := range needles {
					if strings.Contains(info.Name(), n) {
						found = p
					}
				}
			}
			return nil
		}
		data, _ := os.ReadFile(p)
		for _, n := range needles {
			if strings.Contains(info.Name(), n) || bytes.Contains(data, []byte(n)) {
				found = p
			}
		}
		return nil
	})
	return found
}

func TestMirrorLifecycle(t *testing.T) {
	bin := syncthingBin(t)
	owner, mirror, catcher := startNode(t, bin), startNode(t, bin), startNode(t, bin)
	const fid, pw = "casper-test-docs", "a long folder password for tests"
	src := t.TempDir()
	os.MkdirAll(filepath.Join(src, "taxes"), 0o755)
	os.WriteFile(filepath.Join(src, "secret-plans.md"), []byte("receipts are in the blue folder"), 0o644)
	big := make([]byte, 2<<20)
	rand.Read(big)
	os.WriteFile(filepath.Join(src, "taxes", "return.pdf"), big, 0o644)

	ownerFolder := OwnedFolder{ID: fid, Label: "Documents", Path: src, Mirrors: []string{mirror.m.DeviceID()}, Password: pw}
	mustApply(t, owner, Desired{Peers: []Peer{mirror.peer("mirror")}, Owned: []OwnedFolder{ownerFolder}})
	mustApply(t, mirror, Desired{Peers: []Peer{owner.peer("owner")}, Held: []HeldFolder{{ID: fid, Label: "riley", Owners: []string{owner.m.DeviceID()}, Kind: "mirror"}}})

	waitFor(t, "initial mirror", 60*time.Second, func() bool {
		st, _ := owner.m.OwnedStatus(ownerFolder)
		return st.Error == "" && st.AtRiskFiles == 0 && len(st.Peers) == 1 && st.Peers[0].Completion == 100 && st.Peers[0].Connected
	})
	held := mirror.m.HeldPath(fid)
	if f := grepTree(held, "secret-plans", "taxes", "return.pdf", "blue folder"); f != "" {
		t.Fatalf("the mirror can read something: %s", f)
	}
	if DirSize(held) < 2<<20 {
		t.Fatalf("mirror holds too little (%d bytes)", DirSize(held))
	}

	// The mirror sleeps; a change is now at risk, and unprotected.
	mirror.m.Stop()
	os.WriteFile(filepath.Join(src, "new-idea.txt"), []byte("written while the mirror slept"), 0o644)
	waitFor(t, "change seen as unprotected", 30*time.Second, func() bool {
		st, _ := owner.m.OwnedStatus(ownerFolder)
		return st.UnprotectedFiles == 1 && len(st.UnprotectedNames) == 1 && st.UnprotectedNames[0] == "new-idea.txt"
	})

	// A catcher picks up only the gap.
	ownerFolder.Catcher = catcher.m.DeviceID()
	mustApply(t, owner, Desired{Peers: []Peer{mirror.peer("mirror"), catcher.peer("catcher")}, Owned: []OwnedFolder{ownerFolder}})
	mustApply(t, catcher, Desired{Peers: []Peer{owner.peer("owner")}, Held: []HeldFolder{{ID: fid, Label: "riley", Owners: []string{owner.m.DeviceID()}, Kind: "catcher"}}})
	waitFor(t, "catcher to cover the gap", 60*time.Second, func() bool {
		st, risk := owner.m.OwnedStatus(ownerFolder)
		_ = owner.m.MaintainStaging(ownerFolder, risk)
		return st.AtRiskFiles == 1 && st.UnprotectedFiles == 0
	})
	caught := catcher.m.HeldPath(CatchFolderID(fid))
	if size := DirSize(caught); size == 0 || size > 1<<20 {
		t.Fatalf("catcher should hold only the small gap, holds %d bytes", size)
	}
	if f := grepTree(caught, "new-idea", "written while"); f != "" {
		t.Fatalf("the catcher can read something: %s", f)
	}

	// The mirror wakes up; the gap closes and leaves the catcher.
	if err := mirror.m.Start(); err != nil {
		t.Fatal(err)
	}
	mustApply(t, mirror, Desired{Peers: []Peer{owner.peer("owner")}, Held: []HeldFolder{{ID: fid, Label: "riley", Owners: []string{owner.m.DeviceID()}, Kind: "mirror"}}})
	waitFor(t, "mirror catch-up and an empty catcher", 90*time.Second, func() bool {
		st, risk := owner.m.OwnedStatus(ownerFolder)
		_ = owner.m.MaintainStaging(ownerFolder, risk)
		return st.AtRiskFiles == 0 && st.UnprotectedFiles == 0 && len(listFiles(caught)) == 0
	})

	// History: an edit leaves a (still encrypted) version behind.
	os.WriteFile(filepath.Join(src, "secret-plans.md"), []byte("receipts are in the red folder"), 0o644)
	waitFor(t, "a stored version", 60*time.Second, func() bool { return len(ListVersions(held)) >= 1 })
	// The owner can tell which file a version is, and get it back.
	versions := ListVersions(held)
	v := versions[0]
	trailer, err := ReadTrailer(VersionFile(held, v.EncryptedPath, v.At))
	if err != nil {
		t.Fatal(err)
	}
	info, err := DecryptTrailer(trailer, fid, pw)
	if err != nil || info.Name != "secret-plans.md" {
		t.Fatalf("decrypted trailer = %+v, %v", info, err)
	}
	if _, err := DecryptTrailer(trailer, fid, "the wrong password entirely"); err == nil {
		t.Fatal("a wrong password decrypted a trailer")
	}
	data, _ := os.ReadFile(VersionFile(held, v.EncryptedPath, v.At))
	restored, err := DecryptVersion(bin, v.EncryptedPath, data, fid, pw, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(restored); string(got) != "receipts are in the blue folder" {
		t.Fatalf("restored version = %q", got)
	}
	if PruneVersions(held, time.Now()) != 0 {
		t.Fatal("pruning removed a version from the last 7 days")
	}
	if PruneVersions(held, time.Now().Add(40*24*time.Hour)) == 0 || len(ListVersions(held)) != 0 {
		t.Fatal("versions older than 30 days should be pruned")
	}

	// A replacement Mac rebuilds the folder from the mirror alone.
	replacement := startNode(t, bin)
	restoreDir := filepath.Join(t.TempDir(), "Documents")
	restoring := OwnedFolder{ID: fid, Label: "Documents", Path: restoreDir, Mirrors: []string{mirror.m.DeviceID()}, Password: pw, Restore: true}
	mustApply(t, mirror, Desired{Peers: []Peer{owner.peer("owner"), replacement.peer("replacement")},
		Held: []HeldFolder{{ID: fid, Label: "riley", Owners: []string{owner.m.DeviceID(), replacement.m.DeviceID()}, Kind: "mirror"}}})
	mustApply(t, replacement, Desired{Peers: []Peer{mirror.peer("mirror")}, Owned: []OwnedFolder{restoring}})
	waitFor(t, "the replacement Mac to rebuild the folder", 90*time.Second, func() bool {
		got, err := os.ReadFile(filepath.Join(restoreDir, "taxes", "return.pdf"))
		note, _ := os.ReadFile(filepath.Join(restoreDir, "secret-plans.md"))
		return err == nil && bytes.Equal(got, big) && string(note) == "receipts are in the red folder"
	})
}

func TestApplyLeavesNonCasperConfigAlone(t *testing.T) {
	bin := syncthingBin(t)
	n := startNode(t, bin)
	other := t.TempDir()
	if err := n.m.put("/rest/config/folders/personal", stFolder{ID: "personal", Path: other, Type: "sendreceive", Devices: []stFolderDevice{}}); err != nil {
		t.Fatal(err)
	}
	mustApply(t, n, Desired{})
	var folders []stFolder
	n.m.get("/rest/config/folders", &folders)
	for _, f := range folders {
		if f.ID == "personal" {
			return
		}
	}
	t.Fatal("Apply removed a folder Casper doesn't manage")
}

func TestPruneVersionsPolicy(t *testing.T) {
	dir := t.TempDir()
	vdir := filepath.Join(dir, ".stversions", "AB")
	os.MkdirAll(vdir, 0o755)
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.Local)
	stamps := []time.Time{
		now.Add(-1 * time.Hour), now.Add(-2 * time.Hour), // recent: both kept
		now.Add(-10 * 24 * time.Hour), now.Add(-10*24*time.Hour - time.Hour), // same old day: newest kept
		now.Add(-20 * 24 * time.Hour), // old, alone on its day: kept
		now.Add(-40 * 24 * time.Hour), // past 30 days: removed
	}
	for _, s := range stamps {
		os.WriteFile(filepath.Join(vdir, "ENC~"+s.Format("20060102-150405")), []byte("x"), 0o600)
	}
	if removed := PruneVersions(dir, now); removed != 2 {
		t.Fatalf("expected 2 removed, got %d", removed)
	}
	if left := len(ListVersions(dir)); left != 4 {
		t.Fatalf("expected 4 left, got %d", left)
	}
}

func TestCleanupOrphansAfterGrace(t *testing.T) {
	m := New("", t.TempDir(), t.TempDir(), "", t.Logf)
	keep, gone := m.HeldPath("casper-keep"), m.HeldPath("casper-gone")
	os.MkdirAll(keep, 0o700)
	os.MkdirAll(gone, 0o700)
	now := time.Now()
	if d := m.CleanupOrphans(map[string]bool{"casper-keep": true}, 7*24*time.Hour, now); len(d) != 0 {
		t.Fatal("deleted on first sight; should only mark")
	}
	marker := filepath.Join(gone, orphanMarker)
	old := now.Add(-8 * 24 * time.Hour)
	os.Chtimes(marker, old, old)
	if d := m.CleanupOrphans(map[string]bool{"casper-keep": true}, 7*24*time.Hour, now); len(d) != 1 || d[0] != "casper-gone" {
		t.Fatalf("expected casper-gone deleted after grace, got %v", d)
	}
	if _, err := os.Stat(keep); err != nil {
		t.Fatal("a wanted folder was deleted")
	}
}

func TestRelaysSetListenAddresses(t *testing.T) {
	bin := syncthingBin(t)
	n := startNode(t, bin)
	relay := "relay://relay.example:10000/?id=AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"
	mustApply(t, n, Desired{Relays: []string{relay}})
	var opts struct {
		ListenAddresses []string `json:"listenAddresses"`
		RelaysEnabled   bool     `json:"relaysEnabled"`
	}
	n.m.get("/rest/config/options", &opts)
	if !opts.RelaysEnabled || len(opts.ListenAddresses) != 2 || opts.ListenAddresses[1] != relay {
		t.Fatalf("relay not applied: %+v", opts)
	}
	mustApply(t, n, Desired{})
	n.m.get("/rest/config/options", &opts)
	if opts.RelaysEnabled || len(opts.ListenAddresses) != 1 {
		t.Fatalf("relay not removed: %+v", opts)
	}
}
