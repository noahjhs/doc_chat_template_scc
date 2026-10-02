// Package backup implements peer backup (docs/product/scenarios/peer-backup.md)
// on the daemon: both halves of it.
//
//   - As the backup OWNER (owner.go): tar a folder inside the confined home
//     directory, encrypt it with this identity's own age key, split the
//     ciphertext into 4MB chunks, and sign a manifest of the chunks' hashes
//     with this identity's own ed25519 key. Later, reassemble, verify and
//     decrypt a restore into ~/Casper Restores, never overwriting anything.
//   - As the backup PEER (peer.go): store another account's opaque chunks
//     under the Backup Peer grant the host's owner gave them, re-deciding
//     every write fresh against that grant (tier, quota, manifest signature),
//     the same daemon-authoritative posture run_shell_command has. The peer
//     never holds a key that can decrypt anything, and the manifest carries
//     no file names: names and structure live only inside the ciphertext.
//
// casper_service only ever relays ciphertext between the two.
package backup

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"filippo.io/age"
	"filippo.io/age/armor"
)

// Keys are one paired identity's backup keys. The age identity encrypts
// (and is the only thing that can decrypt) that identity's own backups; the
// ed25519 key signs each backup's manifest so a peer can attribute stored
// blobs to whoever made them (docs/product/risk-model.md, point 4).
type Keys struct {
	Age     *age.X25519Identity
	Signing ed25519.PrivateKey
}

// SigningPublicKey is what gets registered with casper_service (and from
// there handed to peers inside a grant) -- base64 standard encoding.
func (k *Keys) SigningPublicKey() string {
	return base64.StdEncoding.EncodeToString(k.Signing.Public().(ed25519.PublicKey))
}

type storedKeys struct {
	Age         string `json:"age"`
	SigningSeed string `json:"signing_seed"`
}

func (k *Keys) marshal() ([]byte, error) {
	return json.Marshal(storedKeys{Age: k.Age.String(), SigningSeed: base64.StdEncoding.EncodeToString(k.Signing.Seed())})
}

func unmarshalKeys(data []byte) (*Keys, error) {
	var s storedKeys
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, err
	}
	id, err := age.ParseX25519Identity(s.Age)
	if err != nil {
		return nil, err
	}
	seed, err := base64.StdEncoding.DecodeString(s.SigningSeed)
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("malformed signing key")
	}
	return &Keys{Age: id, Signing: ed25519.NewKeyFromSeed(seed)}, nil
}

func generateKeys() (*Keys, error) {
	id, err := age.GenerateX25519Identity()
	if err != nil {
		return nil, err
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	return &Keys{Age: id, Signing: priv}, nil
}

// KeyStore persists one secret blob per account. On macOS this is the
// Keychain (keychain_darwin.go); tests use MemoryKeyStore. Save reports
// where the secret actually landed ("icloud" or "local"), so the owner can
// be told whether losing this machine loses the key.
type KeyStore interface {
	Load(account string) ([]byte, error) // (nil, nil) if absent
	Save(account string, secret []byte) (storage string, err error)
}

// LoadOrCreateKeys returns account's existing keys, generating and saving
// a fresh pair the first time. storage is "" when the keys already existed.
func LoadOrCreateKeys(ks KeyStore, account string) (keys *Keys, storage string, err error) {
	data, err := ks.Load(account)
	if err != nil {
		return nil, "", fmt.Errorf("reading backup keys: %w", err)
	}
	if data != nil {
		keys, err := unmarshalKeys(data)
		return keys, "", err
	}
	keys, err = generateKeys()
	if err != nil {
		return nil, "", err
	}
	data, err = keys.marshal()
	if err != nil {
		return nil, "", err
	}
	storage, err = ks.Save(account, data)
	if err != nil {
		return nil, "", fmt.Errorf("saving backup keys: %w", err)
	}
	return keys, storage, nil
}

// ExportKeys returns the keys encrypted to passphrase (age's scrypt
// recipient), ASCII-armored -- the recovery fallback for when the Keychain
// copy isn't synced anywhere (see the plan's Phase 3 fallback). Anyone
// with this text AND the passphrase can decrypt every backup this identity
// made, so it's only ever returned to the owner's own session.
func ExportKeys(k *Keys, passphrase string) (string, error) {
	data, err := k.marshal()
	if err != nil {
		return "", err
	}
	return ExportSecret(data, passphrase)
}

// MarshalKeys is the stored form of keys, for embedding in a recovery kit.
func MarshalKeys(k *Keys) ([]byte, error) { return k.marshal() }

// UnmarshalKeys reverses MarshalKeys.
func UnmarshalKeys(data []byte) (*Keys, error) { return unmarshalKeys(data) }

// ExportSecret encrypts any secret to a passphrase (age scrypt), armored.
func ExportSecret(data []byte, passphrase string) (string, error) {
	if len(passphrase) < 12 {
		return "", errors.New("passphrase must be at least 12 characters")
	}
	r, err := age.NewScryptRecipient(passphrase)
	if err != nil {
		return "", err
	}
	var buf bytes.Buffer
	aw := armor.NewWriter(&buf)
	w, err := age.Encrypt(aw, r)
	if err != nil {
		return "", err
	}
	if _, err := w.Write(data); err != nil {
		return "", err
	}
	if err := w.Close(); err != nil {
		return "", err
	}
	if err := aw.Close(); err != nil {
		return "", err
	}
	return buf.String(), nil
}

// ImportKeys reverses ExportKeys.
func ImportKeys(armored, passphrase string) (*Keys, error) {
	data, err := ImportSecret(armored, passphrase)
	if err != nil {
		return nil, err
	}
	return unmarshalKeys(data)
}

// ImportSecret reverses ExportSecret.
func ImportSecret(armored, passphrase string) ([]byte, error) {
	id, err := age.NewScryptIdentity(passphrase)
	if err != nil {
		return nil, err
	}
	r, err := age.Decrypt(armor.NewReader(bytes.NewReader([]byte(armored))), id)
	if err != nil {
		return nil, fmt.Errorf("couldn't decrypt (wrong passphrase?): %w", err)
	}
	return io.ReadAll(r)
}

// MemoryKeyStore is an in-memory KeyStore for tests.
type MemoryKeyStore struct{ m map[string][]byte }

func NewMemoryKeyStore() *MemoryKeyStore { return &MemoryKeyStore{m: map[string][]byte{}} }

func (s *MemoryKeyStore) Load(account string) ([]byte, error) { return s.m[account], nil }

func (s *MemoryKeyStore) Save(account string, secret []byte) (string, error) {
	s.m[account] = append([]byte(nil), secret...)
	return "memory", nil
}
