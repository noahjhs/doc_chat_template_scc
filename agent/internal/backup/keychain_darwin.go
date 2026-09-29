package backup

import (
	"github.com/keybase/go-keychain"
)

const keychainService = "Casper Backup Keys"

// MacKeychain stores backup keys as a generic-password Keychain item.
//
// It tries a SYNCHRONIZABLE item first (iCloud Keychain copies it,
// end-to-end encrypted, to the owner's other Apple devices -- the scenario's
// preferred recovery path), and falls back to a local login-keychain item
// if macOS refuses. The Principal accepted that fallback on 2026-09-28:
// synchronizable items need the data-protection keychain, which needs
// entitlements this Developer-ID build may not carry. Save reports which
// one actually happened, and the passphrase export (ExportKeys) covers the
// "local" case.
type MacKeychain struct{}

func (MacKeychain) Load(account string) ([]byte, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(keychainService)
	q.SetAccount(account)
	q.SetSynchronizable(keychain.SynchronizableAny)
	q.SetMatchLimit(keychain.MatchLimitOne)
	q.SetReturnData(true)
	results, err := keychain.QueryItem(q)
	if err != nil {
		return nil, err
	}
	if len(results) == 0 {
		return nil, nil
	}
	return results[0].Data, nil
}

func (MacKeychain) Save(account string, secret []byte) (string, error) {
	item := keychain.NewItem()
	item.SetSecClass(keychain.SecClassGenericPassword)
	item.SetService(keychainService)
	item.SetAccount(account)
	item.SetLabel(keychainService)
	item.SetData(secret)
	item.SetAccessible(keychain.AccessibleAfterFirstUnlock)
	item.SetSynchronizable(keychain.SynchronizableYes)
	if err := keychain.AddItem(item); err == nil {
		return "icloud", nil
	}
	item.SetSynchronizable(keychain.SynchronizableDefault)
	if err := keychain.AddItem(item); err != nil {
		return "", err
	}
	return "local", nil
}
