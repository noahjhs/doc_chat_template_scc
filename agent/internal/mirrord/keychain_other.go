//go:build !darwin

package mirrord

import "errors"

type KeychainPasswords struct{}

const KeychainService = "Casper Mirror Keys"

func (KeychainPasswords) Get(string) (string, error) {
	return "", errors.New("mirroring is macOS-only for now")
}
func (KeychainPasswords) Set(string, string) error {
	return errors.New("mirroring is macOS-only for now")
}
func ListAccounts() ([]string, error) { return nil, errors.New("mirroring is macOS-only for now") }
