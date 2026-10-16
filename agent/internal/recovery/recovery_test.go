package recovery

import (
	"bytes"
	"strings"
	"testing"
)

func TestDerivationVectors(t *testing.T) {
	m := bytes.Repeat([]byte{7}, 32)
	// Fixed output (checked against an independent RFC 5869 computation): a change here would orphan every derived folder's data.
	if got, want := FolderPassword(m, "casper-0123456789abcdef"), "TqABagobdSUeGnjYnY6Boybzl5vSikdrRwbwi5HznT8"; got != want {
		t.Fatalf("folder password drifted: %s", got)
	}
	if FolderPassword(m, "casper-a") == FolderPassword(m, "casper-b") {
		t.Fatal("two folders share a password")
	}
	if FolderPassword(m, "casper-a") != FolderPassword(m, "casper-a") {
		t.Fatal("derivation isn't deterministic")
	}
	if bytes.Equal(BundleKey(m), StateKey(m)) || bytes.Equal(BundleKey(m), RecoverySigningSeed(m)) {
		t.Fatal("purposes share a key")
	}
	other := bytes.Repeat([]byte{8}, 32)
	if FolderPassword(m, "casper-a") == FolderPassword(other, "casper-a") {
		t.Fatal("different master keys gave the same password")
	}
}

func TestIdentitiesAreIsolated(t *testing.T) {
	k := &Keys{Store: &MemoryStore{}}
	a, _ := k.Master(Identity("d", "riley"), true)
	b, _ := k.Master(Identity("d", "sam"), true)
	if bytes.Equal(a, b) || len(a) != 32 {
		t.Fatal("identities must each get their own key")
	}
	again, _ := k.Master(Identity("d", "riley"), true)
	if !bytes.Equal(a, again) {
		t.Fatal("the key must be stable")
	}
	if m, _ := k.Master(Identity("d", "cat"), false); m != nil {
		t.Fatal("a Mac that is restoring must never create a key")
	}
	if _, err := k.FolderPassword(Identity("d", "cat"), "casper-a", SchemeDerived1); err != ErrNoMasterKey {
		t.Fatalf("want ErrNoMasterKey, got %v", err)
	}
	if err := k.ImportMaster(Identity("d", "riley"), bytes.Repeat([]byte{1}, 32)); err == nil {
		t.Fatal("importing a different key over an existing one must be refused")
	}
	if err := k.ImportMaster(Identity("d", "riley"), a); err != nil {
		t.Fatal(err)
	}
}

func TestLegacyFolderUsesCarriedPassword(t *testing.T) {
	k := &Keys{Store: &MemoryStore{}, Legacy: func(_, id string) (string, error) { return "old-" + id, nil }}
	if pw, _ := k.FolderPassword("d/riley", "casper-a", SchemeLegacy); pw != "old-casper-a" {
		t.Fatalf("legacy password %q", pw)
	}
}

func TestPathGuard(t *testing.T) {
	k := &Keys{Store: &MemoryStore{}}
	if err := k.CheckPath("d/riley", "casper-a", "/Users/r/Documents"); err != nil {
		t.Fatal(err)
	}
	if err := k.CheckPath("d/riley", "casper-a", "/Users/r/Documents"); err != nil {
		t.Fatal("the same path again is fine")
	}
	if err := k.CheckPath("d/riley", "casper-a", "/Users/r/Photos"); err == nil || !strings.Contains(err.Error(), "already set up") {
		t.Fatalf("a second path for one folder ID must be refused: %v", err)
	}
	if err := k.CheckPath("d/sam", "casper-a", "/Users/s/Photos"); err != nil {
		t.Fatal("another identity is independent")
	}
}

func TestBundleSealing(t *testing.T) {
	m := bytes.Repeat([]byte{3}, 32)
	blob, err := SealBundle(m, LegacyBundle{Passwords: map[string]string{"casper-a": "topsecret"}, BackupKeys: []byte("bk")})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(blob, "topsecret") {
		t.Fatal("plaintext in the blob")
	}
	b, err := OpenBundle(m, blob)
	if err != nil || b.Passwords["casper-a"] != "topsecret" || string(b.BackupKeys) != "bk" {
		t.Fatalf("round trip: %v %+v", err, b)
	}
	if _, err := OpenBundle(bytes.Repeat([]byte{4}, 32), blob); err == nil {
		t.Fatal("opened with the wrong key")
	}
}
