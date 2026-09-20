package config

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Session holds one independent, currently-paired identity's own host
// credentials -- minted once via ExchangePairingToken (see hostpair.go) and
// never derived from the browser's rotating login token again after that.
// DeviceToken authenticates this daemon to the auth service (presence,
// self-verify, self-unpair) for THIS identity; CommandKey authenticates the
// browser to this daemon's local /api/command as THIS identity -- see
// hostpair.go's doc comment for the full split rationale. One daemon can
// hold several of these at once (see Sessions) -- one physical machine
// paired to more than one Casper account simultaneously, each fully
// independent (own credentials, own policy enforcement).
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

// LoadSessions tolerates a missing or corrupt file (returns nil, nil in
// either case) -- mirrors the old single-session LoadSession's broad
// tolerance. Also tolerates the OLD single-object shape this file used to
// have before multi-account pairing (before this, a fresh install's
// session.json was never anything but a single Session; this makes any
// such file left over from an earlier build carry over as one identity
// rather than getting silently dropped as "corrupt").
func LoadSessions() ([]Session, error) {
	path, err := sessionFilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil //nolint:nilerr // missing file is a normal "no saved sessions" case
	}
	var sessions []Session
	if err := json.Unmarshal(data, &sessions); err == nil {
		return validSessions(sessions), nil
	}
	var single Session
	if err := json.Unmarshal(data, &single); err == nil {
		return validSessions([]Session{single}), nil
	}
	return nil, nil //nolint:nilerr // corrupt file is treated the same as no sessions
}

func validSessions(sessions []Session) []Session {
	out := make([]Session, 0, len(sessions))
	for _, s := range sessions {
		if s.Username != "" && s.DeviceToken != "" && s.CommandKey != "" {
			out = append(out, s)
		}
	}
	return out
}

// SaveSessions replaces the whole file with exactly these sessions --
// callers (daemon.go) always pass the complete current set, never a delta.
func SaveSessions(sessions []Session) error {
	path, err := sessionFilePath()
	if err != nil {
		return err
	}
	if sessions == nil {
		sessions = []Session{}
	}
	data, err := json.Marshal(sessions)
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

type VerifyResult int

const (
	VerifyInvalid VerifyResult = iota
	VerifyValid
	VerifyUnknown // couldn't reach the auth service -- callers should trust a cached token rather than force a re-login
)
