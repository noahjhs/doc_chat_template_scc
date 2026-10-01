package setup

import "github.com/keybase/go-keychain"

const keychainService = "Casper Account"

func keychainLoad(account string) ([]byte, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(keychainService)
	q.SetAccount(account)
	q.SetMatchLimit(keychain.MatchLimitOne)
	q.SetReturnData(true)
	results, err := keychain.QueryItem(q)
	if err != nil || len(results) == 0 {
		return nil, err
	}
	return results[0].Data, nil
}

func keychainSave(account string, secret []byte) error {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(keychainService)
	q.SetAccount(account)
	update := keychain.NewItem()
	update.SetData(secret)
	if err := keychain.UpdateItem(q, update); err == nil {
		return nil
	}
	item := keychain.NewItem()
	item.SetSecClass(keychain.SecClassGenericPassword)
	item.SetService(keychainService)
	item.SetAccount(account)
	item.SetLabel(keychainService)
	item.SetData(secret)
	item.SetAccessible(keychain.AccessibleAfterFirstUnlock)
	return keychain.AddItem(item)
}
