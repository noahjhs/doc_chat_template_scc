//go:build !darwin

package recovery

import "errors"

type KeychainStore struct{}

func (KeychainStore) Get(string) ([]byte, error) {
	return nil, errors.New("mirroring is macOS-only for now")
}
func (KeychainStore) Set(string, []byte) error { return errors.New("mirroring is macOS-only for now") }
