package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBaseURL(t *testing.T) {
	cases := map[string]string{
		"localhost:8100":        "http://localhost:8100",
		"127.0.0.1:8100":        "http://127.0.0.1:8100",
		"auth.casperagent.dev":  "https://auth.casperagent.dev",
		"auth.casperagent.dev/": "https://auth.casperagent.dev",
	}
	for input, want := range cases {
		if got := BaseURL(input); got != want {
			t.Errorf("BaseURL(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestSessionRoundTrip(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir) // os.UserConfigDir on darwin is $HOME/Library/Application Support

	if s, err := LoadSession(); err != nil || s != nil {
		t.Fatalf("expected no session initially, got s=%v err=%v", s, err)
	}

	if err := SaveSession("alice", "devtok123", "cmdkey456"); err != nil {
		t.Fatalf("SaveSession: %v", err)
	}
	s, err := LoadSession()
	if err != nil || s == nil {
		t.Fatalf("expected a session after save, got s=%v err=%v", s, err)
	}
	if s.Username != "alice" || s.DeviceToken != "devtok123" || s.CommandKey != "cmdkey456" {
		t.Fatalf("unexpected session contents: %+v", s)
	}

	path, _ := sessionFilePath()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("expected session.json to be 0600, got %o", info.Mode().Perm())
	}

	ClearSession(nil)
	if s, err := LoadSession(); err != nil || s != nil {
		t.Fatalf("expected no session after clear, got s=%v err=%v", s, err)
	}
}

func TestLoadWorkspaceDirs_EmptyWhenNeverSet(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	got, err := LoadWorkspaceDirs()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("expected no directories yet, got %v", got)
	}
}

func TestLoadWorkspaceDirs_ReadsSavedEntriesOnePerLine(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dirA, dirB := t.TempDir(), t.TempDir()

	cfgDir, err := AppConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfgDir, "workspace.txt"), []byte(dirA+"\n"+dirB+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := LoadWorkspaceDirs()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	resolvedA, _ := filepath.EvalSymlinks(dirA)
	resolvedB, _ := filepath.EvalSymlinks(dirB)
	if len(got) != 2 || got[0] != resolvedA || got[1] != resolvedB {
		t.Fatalf("expected [%s %s], got %v", resolvedA, resolvedB, got)
	}
}

func TestLoadWorkspaceDirs_DropsEntriesThatNoLongerExist(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	stillThere := t.TempDir()
	gone := filepath.Join(home, "deleted-dir")

	cfgDir, err := AppConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfgDir, "workspace.txt"), []byte(gone+"\n"+stillThere+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := LoadWorkspaceDirs()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	resolved, _ := filepath.EvalSymlinks(stillThere)
	if len(got) != 1 || got[0] != resolved {
		t.Fatalf("expected only %s to survive, got %v", resolved, got)
	}
}
