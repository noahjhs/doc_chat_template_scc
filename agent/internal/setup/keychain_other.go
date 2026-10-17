//go:build !darwin

package setup

import "casper-agent/internal/secretstore"

const keychainService = "Casper Account"

func keychainLoad(account string) ([]byte, error) {
	s, err := secretstore.Default()
	if err != nil {
		return nil, err
	}
	return s.Get(keychainService, account)
}

func keychainSave(account string, secret []byte) error {
	s, err := secretstore.Default()
	if err != nil {
		return err
	}
	return s.Set(keychainService, account, secret)
}
