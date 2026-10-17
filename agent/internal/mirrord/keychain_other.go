//go:build !darwin

package mirrord

import "casper-agent/internal/secretstore"

const KeychainService = "Casper Mirror Keys"

// KeychainPasswords keeps folder passwords in the sealed-file store (Windows).
type KeychainPasswords struct{}

func (KeychainPasswords) Get(account string) (string, error) {
	s, err := secretstore.Default()
	if err != nil {
		return "", err
	}
	b, err := s.Get(KeychainService, account)
	return string(b), err
}

func (KeychainPasswords) Set(account, password string) error {
	s, err := secretstore.Default()
	if err != nil {
		return err
	}
	return s.Set(KeychainService, account, []byte(password))
}

// ListAccounts returns every stored folder-password account (for the
// recovery kit).
func ListAccounts() ([]string, error) {
	s, err := secretstore.Default()
	if err != nil {
		return nil, err
	}
	return s.List(KeychainService)
}
