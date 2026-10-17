//go:build !darwin

package backup

import "casper-agent/internal/secretstore"

const keychainService = "Casper Backup Keys"

// MacKeychain (named for the Mac, where it's the Keychain) is the sealed-file
// store on Windows. There's no iCloud there, so keys are always "local" and
// the passphrase export (ExportKeys) is the recovery path.
type MacKeychain struct{}

func (MacKeychain) Load(account string) ([]byte, error) {
	s, err := secretstore.Default()
	if err != nil {
		return nil, err
	}
	return s.Get(keychainService, account)
}

func (MacKeychain) Save(account string, secret []byte) (string, error) {
	s, err := secretstore.Default()
	if err != nil {
		return "", err
	}
	if err := s.Set(keychainService, account, secret); err != nil {
		return "", err
	}
	return "local", nil
}
