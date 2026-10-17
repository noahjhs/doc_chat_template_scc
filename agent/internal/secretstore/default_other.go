//go:build !windows

package secretstore

import "errors"

// Default has no store off Windows: macOS uses the Keychain.
func Default() (Store, error) {
	return Store{}, errors.New("secrets are kept in the Keychain on macOS; there's no sealed-file store here")
}
