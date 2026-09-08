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

	if err := SaveSession("alice", "tok123"); err != nil {
		t.Fatalf("SaveSession: %v", err)
	}
	s, err := LoadSession()
	if err != nil || s == nil {
		t.Fatalf("expected a session after save, got s=%v err=%v", s, err)
	}
	if s.Username != "alice" || s.Token != "tok123" {
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

func TestResolveWorkspaceDir_EnvOverride(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("CONTROL_TOOL_WORKSPACE", dir)
	got, err := ResolveWorkspaceDir()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	resolved, _ := filepath.EvalSymlinks(dir)
	if got != resolved {
		t.Fatalf("expected %s, got %s", resolved, got)
	}
}

func TestResolveWorkspaceDir_SavedPointer(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	workspace := t.TempDir()

	cfgDir, err := AppConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cfgDir, "workspace.txt"), []byte(workspace), 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := ResolveWorkspaceDir()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	resolved, _ := filepath.EvalSymlinks(workspace)
	if got != resolved {
		t.Fatalf("expected saved workspace %s, got %s", resolved, got)
	}
}
