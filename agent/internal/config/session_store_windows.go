package config

import "casper-agent/internal/secretstore"

const sessionKeychainService = "Casper Session"

// sealedSessionStore keeps the session list as one DPAPI-sealed item per
// build ("Casper" / "Casper-dev"), the Windows twin of the login-keychain
// item. Any program running as the same person can unseal it (no per-app
// prompt as on the Mac): see docs/product/risk-model.md.
type sealedSessionStore struct{}

func defaultSessionStore() sessionStore { return sealedSessionStore{} }

func (sealedSessionStore) read() ([]byte, error) {
	s, err := secretstore.Default()
	if err != nil {
		return nil, err
	}
	return s.Get(sessionKeychainService, appConfigSubdir())
}

func (sealedSessionStore) write(data []byte) error {
	s, err := secretstore.Default()
	if err != nil {
		return err
	}
	return s.Set(sessionKeychainService, appConfigSubdir(), data)
}
