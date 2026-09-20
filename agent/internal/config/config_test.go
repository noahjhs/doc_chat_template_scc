package config

import (
	"os"
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

func TestAppConfigSubdir(t *testing.T) {
	// embeddedAuthServer is a plain var (go:embed populates it at compile
	// time in a real build), not a const -- safe to mutate directly here
	// and restore afterward.
	original := embeddedAuthServer
	defer func() { embeddedAuthServer = original }()

	t.Run("prod domain", func(t *testing.T) {
		embeddedAuthServer = "auth.casperagent.dev"
		t.Setenv("CONTROL_TOOL_AUTH_DOMAIN", "")
		if got := appConfigSubdir(); got != "Casper" {
			t.Fatalf("expected %q, got %q", "Casper", got)
		}
	})
	t.Run("dev domain", func(t *testing.T) {
		embeddedAuthServer = "dev-auth.casperagent.dev"
		t.Setenv("CONTROL_TOOL_AUTH_DOMAIN", "")
		if got := appConfigSubdir(); got != "Casper-dev" {
			t.Fatalf("expected %q, got %q", "Casper-dev", got)
		}
	})
	t.Run("env override wins over embedded", func(t *testing.T) {
		embeddedAuthServer = "auth.casperagent.dev"
		t.Setenv("CONTROL_TOOL_AUTH_DOMAIN", "dev-auth.casperagent.dev")
		if got := appConfigSubdir(); got != "Casper-dev" {
			t.Fatalf("expected the env override to win, got %q", got)
		}
	})
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
