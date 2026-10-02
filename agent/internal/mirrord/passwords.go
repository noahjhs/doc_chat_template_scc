package mirrord

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"os"
	"sync"
)

// Passwords stores each owned folder's Syncthing encryption password. The
// password never leaves this Mac except inside the person's recovery kit
// (`Casper setup recovery-kit`); casper_service never sees it.
type Passwords interface {
	Get(account string) (string, error) // "" if absent
	Set(account, password string) error
}

// Account names the Keychain item for one identity's folder.
func Account(authDomain, username, folderID string) string {
	return authDomain + "/" + username + "/" + folderID
}

func newPassword() string {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// GetOrCreate returns the folder's password, creating one the first time
// this Mac owns the folder. A Mac restoring a folder never creates one:
// it must come from the recovery kit, or the mirrors' data is unreadable.
func GetOrCreate(p Passwords, account string, create bool) (string, error) {
	pw, err := p.Get(account)
	if err != nil || pw != "" || !create {
		return pw, err
	}
	pw = newPassword()
	return pw, p.Set(account, pw)
}

// MemoryPasswords is for tests and the headless test daemon.
type MemoryPasswords struct {
	mu sync.Mutex
	m  map[string]string
}

func NewMemoryPasswords() *MemoryPasswords { return &MemoryPasswords{m: map[string]string{}} }

func (p *MemoryPasswords) Get(a string) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.m[a], nil
}

func (p *MemoryPasswords) Set(a, pw string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.m[a] = pw
	return nil
}

// FilePasswords keeps passwords in a JSON file -- for the headless test
// daemon only, so an integration test can carry a "recovery kit" to a
// replacement daemon. The real app uses KeychainPasswords.
type FilePasswords struct {
	Path string
	mu   sync.Mutex
}

func (p *FilePasswords) load() map[string]string {
	m := map[string]string{}
	if data, err := os.ReadFile(p.Path); err == nil {
		_ = json.Unmarshal(data, &m)
	}
	return m
}

func (p *FilePasswords) Get(a string) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.load()[a], nil
}

func (p *FilePasswords) Set(a, pw string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	m := p.load()
	m[a] = pw
	data, _ := json.Marshal(m)
	return os.WriteFile(p.Path, data, 0o600)
}
