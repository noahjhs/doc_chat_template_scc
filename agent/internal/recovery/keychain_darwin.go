package recovery

import "github.com/keybase/go-keychain"

// KeychainService holds the master key (account: <authDomain>/<username>).
const KeychainService = "Casper Master Key"

// KeychainStore keeps secrets in the login keychain: local, not synchronizable.
type KeychainStore struct{}

func (KeychainStore) Get(name string) ([]byte, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(KeychainService)
	q.SetAccount(name)
	q.SetMatchLimit(keychain.MatchLimitOne)
	q.SetReturnData(true)
	res, err := keychain.QueryItem(q)
	if err != nil || len(res) == 0 {
		return nil, err
	}
	return res[0].Data, nil
}

func (KeychainStore) Set(name string, data []byte) error {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(KeychainService)
	q.SetAccount(name)
	upd := keychain.NewItem()
	upd.SetData(data)
	if keychain.UpdateItem(q, upd) == nil {
		return nil
	}
	item := keychain.NewItem()
	item.SetSecClass(keychain.SecClassGenericPassword)
	item.SetService(KeychainService)
	item.SetAccount(name)
	item.SetLabel(KeychainService)
	item.SetData(data)
	item.SetSynchronizable(keychain.SynchronizableNo)
	item.SetAccessible(keychain.AccessibleAfterFirstUnlock)
	return keychain.AddItem(item)
}
