package mirrord

import "github.com/keybase/go-keychain"

// KeychainService is where folder passwords live; `Casper setup
// recovery-kit` exports them and `setup restore` imports them.
const KeychainService = "Casper Mirror Keys"

// KeychainPasswords keeps folder passwords in the login keychain (local, not
// synced -- the same choice the backup keys landed on).
type KeychainPasswords struct{}

func (KeychainPasswords) Get(account string) (string, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(KeychainService)
	q.SetAccount(account)
	q.SetMatchLimit(keychain.MatchLimitOne)
	q.SetReturnData(true)
	res, err := keychain.QueryItem(q)
	if err != nil || len(res) == 0 {
		return "", err
	}
	return string(res[0].Data), nil
}

func (KeychainPasswords) Set(account, password string) error {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(KeychainService)
	q.SetAccount(account)
	upd := keychain.NewItem()
	upd.SetData([]byte(password))
	if keychain.UpdateItem(q, upd) == nil {
		return nil
	}
	item := keychain.NewItem()
	item.SetSecClass(keychain.SecClassGenericPassword)
	item.SetService(KeychainService)
	item.SetAccount(account)
	item.SetLabel(KeychainService)
	item.SetData([]byte(password))
	item.SetAccessible(keychain.AccessibleAfterFirstUnlock)
	return keychain.AddItem(item)
}

// ListAccounts returns every stored folder-password account (for the
// recovery kit).
func ListAccounts() ([]string, error) {
	q := keychain.NewItem()
	q.SetSecClass(keychain.SecClassGenericPassword)
	q.SetService(KeychainService)
	q.SetMatchLimit(keychain.MatchLimitAll)
	q.SetReturnAttributes(true)
	res, err := keychain.QueryItem(q)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(res))
	for _, r := range res {
		out = append(out, r.Account)
	}
	return out, nil
}
