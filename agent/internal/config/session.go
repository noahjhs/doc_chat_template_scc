package config

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Session holds this installation's own independent host credentials --
// minted once via ExchangePairingToken (see hostpair.go) and never derived
// from the browser's rotating login token again after that. DeviceToken
// authenticates this daemon to the auth service (presence, self-verify,
// self-unpair); CommandKey authenticates the browser to this daemon's local
// /api/command -- see hostpair.go's doc comment for the full split
// rationale.
type Session struct {
	Username    string `json:"username"`
	DeviceToken string `json:"device_token"`
	CommandKey  string `json:"command_key"`
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
	if s.Username == "" || s.DeviceToken == "" || s.CommandKey == "" {
		return nil, nil
	}
	return &s, nil
}

func SaveSession(username, deviceToken, commandKey string) error {
	path, err := sessionFilePath()
	if err != nil {
		return err
	}
	data, err := json.Marshal(Session{Username: username, DeviceToken: deviceToken, CommandKey: commandKey})
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
