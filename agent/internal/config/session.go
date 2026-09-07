package config

import (
	"bytes"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

type Session struct {
	Username string `json:"username"`
	Token    string `json:"token"`
}

func sessionFilePath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "session.json"), nil
}

// LoadSession tolerates a missing or corrupt file (returns nil, nil in
// either case) -- mirrors load_session()'s broad except clause.
func LoadSession() (*Session, error) {
	path, err := sessionFilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil //nolint:nilerr // missing file is a normal "no saved session" case
	}
	var s Session
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, nil //nolint:nilerr // corrupt file is treated the same as no session
	}
	if s.Username == "" || s.Token == "" {
		return nil, nil
	}
	return &s, nil
}

func SaveSession(username, token string) error {
	path, err := sessionFilePath()
	if err != nil {
		return err
	}
	data, err := json.Marshal(Session{Username: username, Token: token})
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return err
	}
	// Best-effort -- WriteFile's mode is already 0600, but Chmod again
	// covers platforms/filesystems where WriteFile's mode isn't honored
	// exactly (mirrors the Python version's separate os.chmod call).
	_ = os.Chmod(path, 0o600)
	return nil
}

// ClearSession is best-effort: removing a session file that's already gone
// is not an error (mirrors clear_session() catching FileNotFoundError but
// logging anything else).
func ClearSession(logf func(format string, args ...any)) {
	path, err := sessionFilePath()
	if err != nil {
		return
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		if logf != nil {
			logf("clear_session: couldn't remove %s: %s", path, err)
		}
	}
}

type VerifyResult int

const (
	VerifyInvalid VerifyResult = iota
	VerifyValid
	VerifyUnknown // couldn't reach the auth service -- callers should trust a cached token rather than force a re-login
)

// VerifySession POSTs to the auth service's /verify endpoint.
func VerifySession(authDomain, token string) VerifyResult {
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/verify", nil)
	if err != nil {
		return VerifyUnknown
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return VerifyUnknown
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return VerifyUnknown
	}
	var body struct {
		Valid bool `json:"valid"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return VerifyUnknown
	}
	if body.Valid {
		return VerifyValid
	}
	return VerifyInvalid
}

// RevokeSession POSTs to the auth service's /revoke endpoint. Best-effort --
// never returns an error the caller needs to act on.
func RevokeSession(authDomain, token string) {
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/revoke", bytes.NewReader(nil))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+token)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}
