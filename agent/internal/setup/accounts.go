package setup

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"casper-agent/internal/config"
)

// storedAccount is one Casper account set up on this Mac. Username lives in
// a plain index file (accounts.json); the password and session token live in
// the Keychain (see keychain_*.go).
type storedAccount struct {
	Username string `json:"username"`
	Password string `json:"password"`
	Token    string `json:"token"`
}

func indexPath() (string, error) {
	dir, err := config.AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "setup_accounts.json"), nil
}

func loadIndex() []string {
	path, err := indexPath()
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var names []string
	_ = json.Unmarshal(data, &names)
	return names
}

func saveIndex(names []string) error {
	path, err := indexPath()
	if err != nil {
		return err
	}
	sort.Strings(names)
	data, _ := json.Marshal(names)
	return os.WriteFile(path, data, 0o600)
}

func saveAccount(authDomain string, a *storedAccount) error {
	secret, _ := json.Marshal(a)
	if err := keychainSave(authDomain+"/"+a.Username, secret); err != nil {
		return fmt.Errorf("saving to the Keychain: %w", err)
	}
	names := loadIndex()
	for _, n := range names {
		if n == a.Username {
			return nil
		}
	}
	return saveIndex(append(names, a.Username))
}

func loadAccount(authDomain, username string) (*storedAccount, error) {
	data, err := keychainLoad(authDomain + "/" + username)
	if err != nil {
		return nil, err
	}
	if data == nil {
		return nil, nil
	}
	var a storedAccount
	if err := json.Unmarshal(data, &a); err != nil {
		return nil, err
	}
	return &a, nil
}

var errNoAccount = errors.New("no Casper account is set up on this Mac yet -- run `setup account create --username <name>` (or `account login` for an existing one)")

// currentAccount is --account, or the only account on this Mac.
func (c *cli) currentAccount() (*storedAccount, error) {
	names := loadIndex()
	name := c.account
	if name == "" {
		switch len(names) {
		case 0:
			return nil, errNoAccount
		case 1:
			name = names[0]
		default:
			return nil, fmt.Errorf("this Mac has several Casper accounts (%v) -- say which with --account", names)
		}
	}
	a, err := loadAccount(c.authDomain, name)
	if err != nil {
		return nil, err
	}
	if a == nil {
		return nil, fmt.Errorf("no saved credentials for %s on this Mac", name)
	}
	return a, nil
}
