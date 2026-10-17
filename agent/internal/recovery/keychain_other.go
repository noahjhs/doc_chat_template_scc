//go:build !darwin

package recovery

import "casper-agent/internal/secretstore"

// KeychainService holds the master key (account: <authDomain>/<username>).
const KeychainService = "Casper Master Key"

// KeychainStore keeps secrets in the sealed-file store (Windows).
type KeychainStore struct{}

func (KeychainStore) Get(name string) ([]byte, error) {
	s, err := secretstore.Default()
	if err != nil {
		return nil, err
	}
	return s.Get(KeychainService, name)
}

func (KeychainStore) Set(name string, data []byte) error {
	s, err := secretstore.Default()
	if err != nil {
		return err
	}
	return s.Set(KeychainService, name, data)
}
