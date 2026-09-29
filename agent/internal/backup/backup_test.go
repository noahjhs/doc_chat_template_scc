package backup

import (
	"archive/tar"
	"bytes"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func waitReady(t *testing.T, s *Store, id string) map[string]any {
	t.Helper()
	for i := 0; i < 500; i++ {
		st, err := s.PrepareStatus(id)
		if err != nil {
			t.Fatal(err)
		}
		switch st["state"] {
		case "ready":
			return st
		case "failed":
			t.Fatalf("prepare failed: %v", st["error"])
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("prepare never finished")
	return nil
}

type fixture struct {
	owner, peer *Store
	grants      []Grant
	src         string
	home        string
}

func newFixture(t *testing.T, tier string, quota int64) *fixture {
	t.Helper()
	f := &fixture{home: t.TempDir()}
	f.owner = NewStore(t.TempDir(), NewMemoryKeyStore(), "riley", nil)
	f.peer = NewStore(t.TempDir(), NewMemoryKeyStore(), "sam", func() ([]Grant, error) { return f.grants, nil })
	keys, _, err := f.owner.Keys()
	if err != nil {
		t.Fatal(err)
	}
	f.grants = []Grant{{Grantee: "riley", SigningKeys: []string{keys.SigningPublicKey()}, QuotaBytes: quota, WriteTier: tier}}

	f.src = filepath.Join(f.home, "taxes")
	must(t, os.MkdirAll(filepath.Join(f.src, "2025"), 0o755))
	must(t, os.WriteFile(filepath.Join(f.src, "notes.txt"), []byte("hello"), 0o644))
	big := bytes.Repeat([]byte("0123456789abcdef"), (ChunkSize/16)+1000) // > one chunk
	must(t, os.WriteFile(filepath.Join(f.src, "2025", "big.bin"), big, 0o644))
	must(t, os.Symlink("/etc/passwd", filepath.Join(f.src, "escape")))
	return f
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

// push runs the whole owner -> peer transfer the way casper_service does.
func (f *fixture) push(t *testing.T, id string, approved bool) (lastTier string) {
	t.Helper()
	if _, err := f.owner.Prepare(id, f.src); err != nil {
		t.Fatal(err)
	}
	st := waitReady(t, f.owner, id)
	n := st["chunk_count"].(int)
	for i := 0; i < n; i++ {
		content, err := f.owner.ReadChunk(id, i)
		must(t, err)
		var m, sig string
		if i == 0 {
			m, sig = st["manifest"].(string), st["signature"].(string)
		}
		tier, reason, _, err := f.peer.WriteChunk("riley", id, i, content, m, sig, approved)
		must(t, err)
		if tier != "allow" && !(tier == "ask" && approved) {
			if reason != "" {
				t.Logf("chunk %d: %s (%s)", i, tier, reason)
			}
			return tier
		}
		lastTier = tier
	}
	return lastTier
}

func TestRoundTrip_BackupThenRestore(t *testing.T) {
	f := newFixture(t, "allow", 1<<30)
	id := "backup-roundtrip-1"
	if tier := f.push(t, id, false); tier != "allow" {
		t.Fatalf("expected allow, got %s", tier)
	}

	list, err := f.peer.List("riley")
	must(t, err)
	if len(list) != 1 || !list[0].Complete {
		t.Fatalf("expected one complete backup, got %+v", list)
	}

	// Nothing stored on the peer reveals names: grep every stored byte.
	filepath.Walk(f.peer.root, func(p string, info os.FileInfo, err error) error {
		if info != nil && !info.IsDir() {
			data, _ := os.ReadFile(p)
			for _, secret := range []string{"taxes", "notes.txt", "hello", "big.bin"} {
				if bytes.Contains(data, []byte(secret)) {
					t.Errorf("%s on the peer contains %q", p, secret)
				}
			}
		}
		return nil
	})

	m, sig, n, err := f.peer.GetManifest("riley", id)
	must(t, err)
	must(t, f.owner.RestoreBegin(id, m, sig))
	for i := 0; i < n; i++ {
		c, err := f.peer.GetChunk("riley", id, i)
		must(t, err)
		must(t, f.owner.RestoreChunk(id, i, c))
	}
	restores := filepath.Join(f.home, "Casper Restores")
	target, err := f.owner.Unpack(id, restores)
	must(t, err)
	if !strings.HasPrefix(filepath.Base(target), "taxes-") {
		t.Fatalf("unexpected restore folder %s", target)
	}
	got, err := os.ReadFile(filepath.Join(target, "notes.txt"))
	must(t, err)
	if string(got) != "hello" {
		t.Fatalf("notes.txt = %q", got)
	}
	orig, _ := os.ReadFile(filepath.Join(f.src, "2025", "big.bin"))
	restored, err := os.ReadFile(filepath.Join(target, "2025", "big.bin"))
	must(t, err)
	if !bytes.Equal(orig, restored) {
		t.Fatal("big.bin differs after restore")
	}
	if _, err := os.Lstat(filepath.Join(target, "escape")); err == nil {
		t.Fatal("symlink should have been skipped, not backed up")
	}
}

func TestAskTier_WritesNothingUntilApproved(t *testing.T) {
	f := newFixture(t, "ask", 1<<30)
	if tier := f.push(t, "backup-ask-0001", false); tier != "ask" {
		t.Fatalf("expected ask, got %s", tier)
	}
	if list, _ := f.peer.List("riley"); len(list) != 0 {
		t.Fatalf("an unapproved ask stored something: %+v", list)
	}
	must(t, f.owner.Cleanup("backup-ask-0001"))
	if tier := f.push(t, "backup-ask-0001", true); tier != "ask" {
		t.Fatalf("expected approved ask to complete, last tier %s", tier)
	}
	if list, _ := f.peer.List("riley"); len(list) != 1 || !list[0].Complete {
		t.Fatalf("approved backup not stored: %+v", list)
	}
}

func TestAuthorize_QuotaAndMissingGrant(t *testing.T) {
	f := newFixture(t, "allow", 1000)
	tier, reason, err := f.peer.Authorize("riley", "backup-quota-01", 5000)
	must(t, err)
	if tier != "deny" || !strings.Contains(reason, "over quota") {
		t.Fatalf("expected quota deny, got %s %q", tier, reason)
	}
	tier, _, err = f.peer.Authorize("stranger", "backup-quota-01", 1)
	must(t, err)
	if tier != "deny" {
		t.Fatalf("expected deny for a stranger, got %s", tier)
	}
	// Writes are re-checked against the quota too, not just Authorize.
	if tier := f.push(t, "backup-quota-02", false); tier != "deny" {
		t.Fatalf("expected write-time quota deny, got %s", tier)
	}
}

func TestWriteChunk_RejectsTamperingAndForeignManifests(t *testing.T) {
	f := newFixture(t, "allow", 1<<30)
	id := "backup-tamper-01"
	_, err := f.owner.Prepare(id, f.src)
	must(t, err)
	st := waitReady(t, f.owner, id)
	content, _ := f.owner.ReadChunk(id, 0)
	raw, _ := base64.StdEncoding.DecodeString(content)
	raw[10] ^= 0xff
	tier, reason, _, err := f.peer.WriteChunk("riley", id, 0, base64.StdEncoding.EncodeToString(raw), st["manifest"].(string), st["signature"].(string), false)
	must(t, err)
	if tier != "deny" || !strings.Contains(reason, "manifest") {
		t.Fatalf("tampered chunk: got %s %q", tier, reason)
	}

	// A manifest signed by someone else's key is refused.
	mallory := NewStore(t.TempDir(), NewMemoryKeyStore(), "mallory", nil)
	_, err = mallory.Prepare("backup-tamper-02", f.src)
	must(t, err)
	mst := waitReady(t, mallory, "backup-tamper-02")
	mc, _ := mallory.ReadChunk("backup-tamper-02", 0)
	tier, _, _, err = f.peer.WriteChunk("riley", "backup-tamper-02", 0, mc, mst["manifest"].(string), mst["signature"].(string), false)
	must(t, err)
	if tier != "deny" {
		t.Fatalf("foreign manifest: expected deny, got %s", tier)
	}
}

func TestRestore_OnlyOnTheHostHoldingTheKey(t *testing.T) {
	f := newFixture(t, "allow", 1<<30)
	id := "backup-otherkey-1"
	f.push(t, id, false)
	m, sig, _, err := f.peer.GetManifest("riley", id)
	must(t, err)
	other := NewStore(t.TempDir(), NewMemoryKeyStore(), "riley-other-laptop", nil)
	if err := other.RestoreBegin(id, m, sig); err == nil || !strings.Contains(err.Error(), "different key") {
		t.Fatalf("expected a different-key refusal, got %v", err)
	}
}

func TestRevocation_GraceThenPurge(t *testing.T) {
	f := newFixture(t, "allow", 1<<30)
	id := "backup-revoke-001"
	f.push(t, id, false)

	revoked := time.Now().Add(-time.Hour)
	f.grants[0].RevokedAt = &revoked
	f.peer.RefreshGrants()
	if tier, _, _ := f.peer.Authorize("riley", "backup-revoke-002", 1); tier != "deny" {
		t.Fatalf("revoked grant must not accept writes, got %s", tier)
	}
	if list, err := f.peer.List("riley"); err != nil || len(list) != 1 {
		t.Fatalf("within grace, riley can still see their backup: %v %v", list, err)
	}

	expired := time.Now().Add(-RevocationGrace - time.Hour)
	f.grants[0].RevokedAt = &expired
	purged, err := f.peer.RefreshGrants()
	must(t, err)
	if len(purged) != 1 {
		t.Fatalf("expected riley's data purged, got %v", purged)
	}
	if _, err := os.Stat(f.peer.peerDir("riley")); !os.IsNotExist(err) {
		t.Fatal("purged data still on disk")
	}
}

func TestExtractTar_RejectsEscapes(t *testing.T) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	tw.WriteHeader(&tar.Header{Name: "taxes/", Typeflag: tar.TypeDir, Mode: 0o755})
	tw.WriteHeader(&tar.Header{Name: "taxes/../../evil", Typeflag: tar.TypeReg, Mode: 0o644, Size: 1})
	tw.Write([]byte("x"))
	tw.Close()
	if _, err := extractTar(&buf, t.TempDir(), "stamp"); err == nil {
		t.Fatal("expected an escaping entry to be rejected")
	}
}

func TestExportImportKeys(t *testing.T) {
	keys, err := generateKeys()
	must(t, err)
	armored, err := ExportKeys(keys, "correct horse battery")
	must(t, err)
	if _, err := ImportKeys(armored, "wrong passphrase here"); err == nil {
		t.Fatal("wrong passphrase accepted")
	}
	back, err := ImportKeys(armored, "correct horse battery")
	must(t, err)
	if back.SigningPublicKey() != keys.SigningPublicKey() || back.Age.String() != keys.Age.String() {
		t.Fatal("keys changed across export/import")
	}
	if _, err := ExportKeys(keys, "short"); err == nil {
		t.Fatal("short passphrase accepted")
	}
}

func TestPrepare_RefusesOverTheAlphaLimit(t *testing.T) {
	s := NewStore(t.TempDir(), NewMemoryKeyStore(), "riley", nil)
	dir := t.TempDir()
	f, _ := os.Create(filepath.Join(dir, "huge"))
	f.Truncate(MaxBackupBytes + 1) // sparse: no real disk use
	f.Close()
	if _, err := s.Prepare("backup-toobig-01", dir); err == nil || !strings.Contains(err.Error(), "alpha limit") {
		t.Fatalf("expected the size cap, got %v", err)
	}
}
