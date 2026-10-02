package config

import "github.com/keybase/go-keychain"

const sessionKeychainService = "Casper Session"

// keychainSessionStore keeps the session list as one login-keychain item
// per build ("Casper" / "Casper-dev"). Items created by Casper.app are
// readable by Casper.app without a prompt; any other program (`security`,
// a script) gets macOS's own "allow access?" prompt, which only the person
// can answer.
type keychainSessionStore struct{}

func defaultSessionStore() sessionStore { return keychainSessionStore{} }

func (keychainSessionStore) read() ([]byte, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(sessionKeychainService)
	q.SetAccount(appConfigSubdir())
	q.SetMatchLimit(keychain.MatchLimitOne)
	q.SetReturnData(true)
	res, err := keychain.QueryItem(q)
	if err != nil || len(res) == 0 {
		return nil, err
	}
	return res[0].Data, nil
}

func (keychainSessionStore) write(data []byte) error {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(sessionKeychainService)
	q.SetAccount(appConfigSubdir())
	upd := keychain.NewItem()
	upd.SetData(data)
	if keychain.UpdateItem(q, upd) == nil {
		return nil
	}
	item := keychain.NewItem()
	item.SetSecClass(keychain.SecClassGenericPassword)
	item.SetService(sessionKeychainService)
	item.SetAccount(appConfigSubdir())
	item.SetLabel(sessionKeychainService)
	item.SetData(data)
	item.SetAccessible(keychain.AccessibleAfterFirstUnlock)
	return keychain.AddItem(item)
}
