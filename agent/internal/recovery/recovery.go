// Package recovery holds a person's master key and what is derived from it
// (docs/product/recovery-v2-design.md, 2.2). One 32-byte key per paired
// identity lives in this Mac's Keychain; every new folder's Syncthing
// password is calculated from it, so a kit or a keeper that holds the key
// opens everything, including folders mirrored later. casper_service never
// sees the key, only a bundle sealed under a key derived from it.
package recovery

import (
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"

	"golang.org/x/crypto/chacha20poly1305"
)

const (
	SchemeLegacy   = "legacy"
	SchemeDerived1 = "derived1"

	hkdfSalt = "casper-v2"
)

// ErrNoMasterKey: this Mac hasn't recovered (or made) the master key yet.
var ErrNoMasterKey = errors.New("this Mac doesn't have the master key yet")

// KeyStore keeps small secrets by name: the login Keychain on a Mac, a file
// for the headless test daemon.
type KeyStore interface {
	Get(name string) ([]byte, error) // nil if absent
	Set(name string, data []byte) error
}

// Identity names one paired account on one Casper deployment.
func Identity(authDomain, username string) string { return authDomain + "/" + username }

func derive(master []byte, info string, n int) []byte {
	out, err := hkdf.Key(sha256.New, master, []byte(hkdfSalt), info, n)
	if err != nil {
		panic(err) // only for an absurd length
	}
	return out
}

// FolderPassword is the Syncthing encryption password of a folder: 32 bytes,
// base64url like the random ones before it.
func FolderPassword(master []byte, folderID string) string {
	return base64.RawURLEncoding.EncodeToString(derive(master, "folder-password\x00"+folderID, 32))
}

// BundleKey seals the legacy bundle; StateKey will seal the owner's recovery
// state (P3). RecoverySigningSeed is the ed25519 seed for recovery records.
func BundleKey(master []byte) []byte {
	return derive(master, "legacy-bundle", chacha20poly1305.KeySize)
}
func StateKey(master []byte) []byte {
	return derive(master, "recovery-state", chacha20poly1305.KeySize)
}
func RecoverySigningSeed(master []byte) []byte { return derive(master, "recovery-signing", 32) }

// LegacyBundle freezes the folders that already had random passwords (and
// the backup keys) when the master key was made. It never grows.
type LegacyBundle struct {
	Passwords  map[string]string `json:"passwords"` // folder ID -> password
	BackupKeys []byte            `json:"backup_keys,omitempty"`
}

// SealBundle encrypts the bundle (XChaCha20-Poly1305) for casper_service to keep.
func SealBundle(master []byte, b LegacyBundle) (string, error) {
	plain, _ := json.Marshal(b)
	aead, err := chacha20poly1305.NewX(BundleKey(master))
	if err != nil {
		return "", err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(aead.Seal(nonce, nonce, plain, []byte("casper legacy bundle v1"))), nil
}

// OpenBundle reverses SealBundle; it fails if the key is wrong or the blob was altered.
func OpenBundle(master []byte, blob string) (LegacyBundle, error) {
	raw, err := base64.StdEncoding.DecodeString(blob)
	aead, kerr := chacha20poly1305.NewX(BundleKey(master))
	if err != nil || kerr != nil || len(raw) < aead.NonceSize() {
		return LegacyBundle{}, errors.New("the legacy bundle is malformed")
	}
	plain, err := aead.Open(nil, raw[:aead.NonceSize()], raw[aead.NonceSize():], []byte("casper legacy bundle v1"))
	if err != nil {
		return LegacyBundle{}, errors.New("the legacy bundle doesn't open with this key")
	}
	var b LegacyBundle
	if err := json.Unmarshal(plain, &b); err != nil {
		return LegacyBundle{}, err
	}
	return b, nil
}

// Keys is the daemon's key source: master keys per identity, folder
// passwords from them, and the (folder, path) guard.
type Keys struct {
	Store KeyStore
	// Legacy returns a legacy folder's stored random password ("" if absent).
	Legacy func(identity, folderID string) (string, error)
	mu     sync.Mutex
}

// Master returns the identity's master key. With create, a new one is made
// if there is none (never on a Mac that is restoring); otherwise nil.
func (k *Keys) Master(identity string, create bool) (key []byte, err error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if m, err := k.Store.Get(identity); err != nil || m != nil {
		return m, err
	}
	if !create {
		return nil, nil
	}
	m := make([]byte, 32)
	if _, err := rand.Read(m); err != nil {
		return nil, err
	}
	return m, k.Store.Set(identity, m)
}

// ImportMaster stores a master key recovered from a kit. A Mac that already
// has a different one refuses: the two would open different folders.
func (k *Keys) ImportMaster(identity string, m []byte) error {
	if len(m) != 32 {
		return errors.New("the kit's master key is malformed")
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if have, err := k.Store.Get(identity); err != nil {
		return err
	} else if have != nil {
		if string(have) != string(m) {
			return errors.New("this Mac already has a different Casper master key for this account")
		}
		return nil
	}
	return k.Store.Set(identity, m)
}

// FolderPassword returns a folder's password: derived from the master key
// for "derived1", or the carried random one for "legacy" (or "").
func (k *Keys) FolderPassword(identity, folderID, scheme string) (string, error) {
	if scheme == SchemeDerived1 {
		m, err := k.Master(identity, false)
		if err != nil {
			return "", err
		}
		if m == nil {
			return "", ErrNoMasterKey
		}
		return FolderPassword(m, folderID), nil
	}
	if k.Legacy == nil {
		return "", nil
	}
	return k.Legacy(identity, folderID)
}

// CheckPath records the first path a folder ID was derived for and refuses
// any other: a server that reused one ID for two folders would otherwise
// make them share a password.
func (k *Keys) CheckPath(identity, folderID, path string) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	name := identity + "#paths"
	seen := map[string]string{}
	if data, err := k.Store.Get(name); err != nil {
		return err
	} else if data != nil {
		_ = json.Unmarshal(data, &seen)
	}
	if p, ok := seen[folderID]; ok {
		if p != path {
			return fmt.Errorf("folder %s was already set up at %s, not %s", folderID, p, path)
		}
		return nil
	}
	seen[folderID] = path
	data, _ := json.Marshal(seen)
	return k.Store.Set(name, data)
}

// FileStore keeps secrets in a JSON file -- for the headless test daemon only.
type FileStore struct {
	Path string
	mu   sync.Mutex
}

func (s *FileStore) load() map[string][]byte {
	m := map[string][]byte{}
	if data, err := os.ReadFile(s.Path); err == nil {
		_ = json.Unmarshal(data, &m)
	}
	return m
}

func (s *FileStore) Get(name string) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.load()[name], nil
}

func (s *FileStore) Set(name string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	m := s.load()
	m[name] = data
	out, _ := json.Marshal(m)
	return os.WriteFile(s.Path, out, 0o600)
}

// MemoryStore is for unit tests.
type MemoryStore struct {
	mu sync.Mutex
	m  map[string][]byte
}

func (s *MemoryStore) Get(name string) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.m[name], nil
}

func (s *MemoryStore) Set(name string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.m == nil {
		s.m = map[string][]byte{}
	}
	s.m[name] = data
	return nil
}
