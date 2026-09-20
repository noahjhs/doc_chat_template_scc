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

func TestSessionsRoundTrip(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir) // os.UserConfigDir on darwin is $HOME/Library/Application Support

	if s, err := LoadSessions(); err != nil || len(s) != 0 {
		t.Fatalf("expected no sessions initially, got s=%v err=%v", s, err)
	}

	alice := Session{Username: "alice", DeviceToken: "devtok123", CommandKey: "cmdkey456"}
	bob := Session{Username: "bob", DeviceToken: "devtok789", CommandKey: "cmdkey789"}
	if err := SaveSessions([]Session{alice, bob}); err != nil {
		t.Fatalf("SaveSessions: %v", err)
	}

	sessions, err := LoadSessions()
	if err != nil || len(sessions) != 2 {
		t.Fatalf("expected 2 sessions after save, got sessions=%v err=%v", sessions, err)
	}
	byUsername := map[string]Session{}
	for _, s := range sessions {
		byUsername[s.Username] = s
	}
	if byUsername["alice"] != alice || byUsername["bob"] != bob {
		t.Fatalf("unexpected session contents: %+v", sessions)
	}

	path, _ := sessionFilePath()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Errorf("expected session.json to be 0600, got %o", info.Mode().Perm())
	}

	// Removing one identity (mirrors daemon.go's removeSessionFromDisk)
	// leaves the other untouched.
	if err := SaveSessions([]Session{bob}); err != nil {
		t.Fatalf("SaveSessions: %v", err)
	}
	sessions, err = LoadSessions()
	if err != nil || len(sessions) != 1 || sessions[0] != bob {
		t.Fatalf("expected only bob to remain, got sessions=%v err=%v", sessions, err)
	}

	if err := SaveSessions(nil); err != nil {
		t.Fatalf("SaveSessions(nil): %v", err)
	}
	if s, err := LoadSessions(); err != nil || len(s) != 0 {
		t.Fatalf("expected no sessions after clearing, got s=%v err=%v", s, err)
	}
}

// TestLoadSessions_TolerantOfOldSingleObjectShape confirms a session.json
// left over from before multi-account pairing (a single JSON object, not
// an array) still loads as one identity rather than being treated as
// corrupt.
func TestLoadSessions_TolerantOfOldSingleObjectShape(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)

	path, err := sessionFilePath() // creates AppConfigDir as a side effect
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"username":"carol","device_token":"dt","command_key":"ck"}`), 0o600); err != nil {
		t.Fatal(err)
	}

	sessions, err := LoadSessions()
	if err != nil || len(sessions) != 1 {
		t.Fatalf("expected exactly one session from the old shape, got sessions=%v err=%v", sessions, err)
	}
	if sessions[0].Username != "carol" || sessions[0].DeviceToken != "dt" || sessions[0].CommandKey != "ck" {
		t.Fatalf("unexpected session contents: %+v", sessions[0])
	}
}

func TestLoadSessions_TreatsCorruptFileAsEmpty(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)

	path, err := sessionFilePath() // creates AppConfigDir as a side effect
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("not json"), 0o600); err != nil {
		t.Fatal(err)
	}

	if sessions, err := LoadSessions(); err != nil || len(sessions) != 0 {
		t.Fatalf("expected a corrupt file to load as empty, got sessions=%v err=%v", sessions, err)
	}
}
