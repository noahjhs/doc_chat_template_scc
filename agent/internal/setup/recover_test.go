package setup

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"casper-agent/internal/config"
	"casper-agent/internal/mirrord"
	"casper-agent/internal/recovery"
)

func TestKitFileRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "kit.txt")
	kit := NewKit("casper.test", "riley", nil, map[string]string{"casper-1": "pw1", "casper-2": "pw2"})
	if err := WriteKitFile(path, kit, "short"); err == nil {
		t.Fatal("a short passphrase should be refused")
	}
	if err := WriteKitFile(path, kit, "a long enough passphrase"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), "pw1") || strings.Contains(string(raw), "casper-1") {
		t.Fatal("the kit file leaks its contents")
	}
	if _, err := ReadKitFile(path, "the wrong passphrase!"); err == nil || !strings.Contains(err.Error(), "wrong passphrase") {
		t.Fatalf("wrong passphrase: %v", err)
	}
	got, err := ReadKitFile(path, "a long enough passphrase")
	if err != nil || got.Account != "riley" || got.MirrorPasswords["casper-2"] != "pw2" {
		t.Fatalf("got %+v, %v", got, err)
	}
}

func TestRestorePlanPlacesFoldersSafely(t *testing.T) {
	home := t.TempDir()
	_ = os.MkdirAll(filepath.Join(home, "Pictures"), 0o755)
	_ = os.WriteFile(filepath.Join(home, "Pictures", "x.jpg"), []byte("x"), 0o644)
	_ = os.MkdirAll(filepath.Join(home, "Music"), 0o755) // present but empty
	kit := NewKit("d", "riley", nil, map[string]string{"a": "p", "b": "p", "c": "p", "d": "p", "e": "p"})
	folders := []config.RestorableFolder{
		{ID: "a", Label: "Documents", Path: filepath.Join(home, "Documents")}, // missing here: original place
		{ID: "b", Label: "Pictures", Path: filepath.Join(home, "Pictures")},   // in use here: not overwritten
		{ID: "c", Label: "Music", Path: filepath.Join(home, "Music")},         // empty here: original place
		{ID: "d", Label: "Work", Path: "/Users/someone-else/Work"},            // outside this home
		{ID: "z", Label: "NotInKit", Path: filepath.Join(home, "NotInKit")},   // no password in the kit
	}
	where, labels, uncovered := PlanRestore(folders, kit, home)
	want := map[string]string{
		"a": filepath.Join(home, "Documents"),
		"b": filepath.Join(home, "Casper Restores", "Pictures"),
		"c": filepath.Join(home, "Music"),
		"d": filepath.Join(home, "Casper Restores", "Work"),
	}
	for id, p := range want {
		if where[id] != p {
			t.Errorf("%s: got %q want %q", id, where[id], p)
		}
	}
	if len(where) != 4 || len(labels) != 4 || len(uncovered) != 1 || uncovered[0].ID != "z" {
		t.Fatalf("where=%v labels=%v uncovered=%v", where, labels, uncovered)
	}
}

// Recover imports the kit's passwords, asks Casper what could come back,
// needs the person's yes, and only then starts the restore.
func TestRecoverNeedsTheYes(t *testing.T) {
	var started []map[string]string
	var unlocked bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Action   string              `json:"action"`
			Unlocked bool                `json:"unlocked"`
			Folders  []map[string]string `json:"folders"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if r.URL.Path != "/hosts/restore" || r.Header.Get("Authorization") != "Bearer dt" {
			http.Error(w, "no", 404)
			return
		}
		if body.Action == "start" {
			started = body.Folders
			_, _ = w.Write([]byte(`{}`))
			return
		}
		unlocked = unlocked || body.Unlocked
		_, _ = w.Write([]byte(`{"folders":[{"id":"casper-1","label":"Documents","path":"/Users/old/Documents"}]}`))
	}))
	defer srv.Close()
	domain := strings.TrimPrefix(srv.URL, "http://")
	home := t.TempDir()
	pw := mirrord.NewMemoryPasswords()
	kit := NewKit(domain, "riley", nil, map[string]string{"casper-1": "secret"})
	args := RecoverArgs{AuthDomain: domain, Username: "riley", DeviceToken: "dt", Home: home, Kit: kit, Passwords: pw}

	args.Confirm = func(string) bool { return false }
	if _, err := Recover(args); err == nil || len(started) != 0 {
		t.Fatalf("declined: err=%v started=%v", err, started)
	}
	if !unlocked {
		t.Fatal("Casper wasn't told the kit was opened")
	}
	if got, _ := pw.Get(mirrord.Account(domain, "riley", "casper-1")); got != "secret" {
		t.Fatalf("password not imported: %q", got)
	}
	var summary string
	args.Confirm = func(s string) bool { summary = s; return true }
	if _, err := Recover(args); err != nil {
		t.Fatal(err)
	}
	if len(started) != 1 || started[0]["id"] != "casper-1" || started[0]["path"] != filepath.Join(home, "Casper Restores", "Documents") { // /Users/old is outside this home
		t.Fatalf("started %v", started)
	}
	if !strings.Contains(summary, "Documents") {
		t.Fatalf("the person wasn't told what comes back: %q", summary)
	}
	wrong := args
	wrong.Kit.Account = "someone-else"
	if _, err := Recover(wrong); err == nil || !strings.Contains(err.Error(), "someone-else") {
		t.Fatalf("a kit for another account should be refused: %v", err)
	}
}

// A version-3 kit holds the master key: it round-trips, covers a folder
// created after the kit was made (derived1) and, via the sealed bundle on
// casper_service, a legacy folder the kit carries no plaintext for.
func TestKitV3CoversNewAndLegacyFolders(t *testing.T) {
	master := make([]byte, 32)
	master[0] = 9
	path := filepath.Join(t.TempDir(), "kit.txt")
	kit := NewKitV3("d", "riley", master, nil, map[string]string{"casper-old1": "pw-old1"})
	if err := WriteKitFile(path, kit, "a long enough passphrase"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), "make a new kit") || strings.Contains(string(raw), "Make a new kit") {
		t.Fatal("a v3 kit never goes stale; it shouldn't tell the person to remake it")
	}
	got, err := ReadKitFile(path, "a long enough passphrase")
	if err != nil || string(got.MasterKey) != string(master) || got.LegacyBundle["casper-old1"] != "pw-old1" {
		t.Fatalf("got %+v, %v", got, err)
	}

	blob, _ := recovery.SealBundle(master, recovery.LegacyBundle{Passwords: map[string]string{"casper-old2": "pw-old2"}})
	var started []map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/hosts/legacy-bundle":
			_, _ = w.Write([]byte(`{"blob":"` + blob + `"}`))
		case "/hosts/restore":
			var body struct {
				Action  string              `json:"action"`
				Folders []map[string]string `json:"folders"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.Action == "start" {
				started = body.Folders
				_, _ = w.Write([]byte(`{}`))
				return
			}
			_, _ = w.Write([]byte(`{"folders":[
			 {"id":"casper-old1","label":"A","path":"/x/A","key_scheme":"legacy"},
			 {"id":"casper-old2","label":"B","path":"/x/B","key_scheme":"legacy"},
			 {"id":"casper-new","label":"C","path":"/x/C","key_scheme":"derived1"}]}`))
		}
	}))
	defer srv.Close()
	domain := strings.TrimPrefix(srv.URL, "http://")
	pw := mirrord.NewMemoryPasswords()
	store := &recovery.MemoryStore{}
	kit.AuthDomain = domain
	if _, err := Recover(RecoverArgs{AuthDomain: domain, Username: "riley", DeviceToken: "dt", Home: t.TempDir(), Kit: got,
		Passwords: pw, Keys: NewKeys(store, pw, domain), Confirm: func(string) bool { return true }}); err != nil {
		t.Fatal(err)
	}
	if len(started) != 3 {
		t.Fatalf("all three folders should restore: %v", started)
	}
	k := NewKeys(store, pw, domain)
	id := recovery.Identity(domain, "riley")
	if p, _ := k.FolderPassword(id, "casper-new", recovery.SchemeDerived1); p != recovery.FolderPassword(master, "casper-new") {
		t.Fatal("the new folder's password isn't derived from the kit's key")
	}
	if p, _ := k.FolderPassword(id, "casper-old2", recovery.SchemeLegacy); p != "pw-old2" {
		t.Fatalf("the legacy folder wasn't opened via the sealed bundle: %q", p)
	}
	if p, _ := k.FolderPassword(id, "casper-old1", recovery.SchemeLegacy); p != "pw-old1" {
		t.Fatalf("the kit's own legacy password: %q", p)
	}
}
