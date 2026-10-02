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

// sessionStore persists the raw session list. On macOS that's the login
// Keychain (session_keychain_darwin.go): the device token and command key
// it holds are exactly what a local process (say, an agent with a shell)
// would need to impersonate this daemon to casper_service -- for example to
// forge the person's own consent (docs/user-flows/mirroring.md, Known
// issues) -- so they don't belong in a plain file. Tests swap in the file
// store.
type sessionStore interface {
	read() ([]byte, error) // nil if nothing saved
	write([]byte) error
}

type fileSessionStore struct{}

func (fileSessionStore) read() ([]byte, error) {
	path, err := sessionFilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil //nolint:nilerr // missing file is a normal "no saved sessions" case
	}
	return data, nil
}

func (fileSessionStore) write(data []byte) error {
	path, err := sessionFilePath()
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return err
	}
	_ = os.Chmod(path, 0o600)
	return nil
}

var sessionBackend sessionStore = defaultSessionStore()

// LoadSessions tolerates missing or corrupt data (returns nil, nil in
// either case), and the old single-object shape from before multi-account
// pairing. On first run after an upgrade it moves a leftover session.json
// into the Keychain and deletes the file.
func LoadSessions() ([]Session, error) {
	migrateSessionFile()
	data, err := sessionBackend.read()
	if err != nil || data == nil {
		return nil, err
	}
	var sessions []Session
	if err := json.Unmarshal(data, &sessions); err == nil {
		return validSessions(sessions), nil
	}
	var single Session
	if err := json.Unmarshal(data, &single); err == nil {
		return validSessions([]Session{single}), nil
	}
	return nil, nil //nolint:nilerr // corrupt data is treated the same as no sessions
}

func migrateSessionFile() {
	if _, isFile := sessionBackend.(fileSessionStore); isFile {
		return
	}
	data, _ := fileSessionStore{}.read()
	if data == nil {
		return
	}
	if err := sessionBackend.write(data); err != nil {
		return // keep the file; try again next launch
	}
	if path, err := sessionFilePath(); err == nil {
		_ = os.Remove(path)
	}
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

// SaveSessions replaces the saved set with exactly these sessions --
// callers (daemon.go) always pass the complete current set, never a delta.
func SaveSessions(sessions []Session) error {
	if sessions == nil {
		sessions = []Session{}
	}
	data, err := json.Marshal(sessions)
	if err != nil {
		return err
	}
	return sessionBackend.write(data)
}

type VerifyResult int

const (
	VerifyInvalid VerifyResult = iota
	VerifyValid
	VerifyUnknown // couldn't reach the auth service -- callers should trust a cached token rather than force a re-login
)
