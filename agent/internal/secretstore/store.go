// Package secretstore keeps Casper's small secrets (session list, account
// password, folder passwords, master key, backup keys) as sealed files on
// platforms with no Keychain. On Windows the sealing is DPAPI with the
// current-user scope. Like Credential Manager, that unlocks for any program
// running as the same person: unlike the Mac Keychain there is no per-app
// access prompt (docs/product/risk-model.md, "Secrets on Windows").
//
// Layout: <root>\secrets\<service>\<hex(account)>, one sealed blob per item.
package secretstore

import (
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// Sealer protects bytes for storage at rest.
type Sealer interface {
	Seal(plain []byte) ([]byte, error)
	Unseal(sealed []byte) ([]byte, error)
}

// Store is a directory of sealed items, grouped by service.
type Store struct {
	Root   string // the directory that holds "secrets"
	Sealer Sealer
}

func (s Store) dir(service string) string {
	return filepath.Join(s.Root, "secrets", hex.EncodeToString([]byte(service)))
}

func (s Store) path(service, account string) string {
	return filepath.Join(s.dir(service), hex.EncodeToString([]byte(account)))
}

// Get returns the item, or nil, nil if there isn't one.
func (s Store) Get(service, account string) ([]byte, error) {
	sealed, err := os.ReadFile(s.path(service, account))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return s.Sealer.Unseal(sealed)
}

// Set creates or replaces the item; the file is swapped in whole, so a crash
// never leaves half an item.
func (s Store) Set(service, account string, data []byte) error {
	sealed, err := s.Sealer.Seal(data)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(s.dir(service), 0o700); err != nil {
		return err
	}
	path := s.path(service, account)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, sealed, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// List returns every account stored under the service.
func (s Store) List(service string) ([]string, error) {
	entries, err := os.ReadDir(s.dir(service))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() || strings.HasSuffix(e.Name(), ".tmp") {
			continue
		}
		if b, err := hex.DecodeString(e.Name()); err == nil {
			out = append(out, string(b))
		}
	}
	return out, nil
}
