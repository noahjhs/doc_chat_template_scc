package backup

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

const (
	// ChunkSize is how much ciphertext one backup_*_chunk call carries --
	// ~7.1MB once base64'd into the request JSON and again into the relay's
	// frame, comfortably under the tunnel's 16MB frame limit.
	ChunkSize = 4 << 20
	// MaxBackupBytes is the alpha's per-backup cap (plaintext), agreed
	// 2026-09-28.
	MaxBackupBytes = 2 << 30
	// RevocationGrace is how long a revoked peer can still restore or
	// delete what it already stored before the host purges it.
	RevocationGrace = 7 * 24 * time.Hour
	// grantsMaxAge bounds how stale a cached grant list may be before a
	// peer-side decision refetches it. casper_service also asks for an
	// immediate refresh whenever a grant changes (refresh_grants).
	grantsMaxAge = 60 * time.Second
)

// UserError is a well-formed request the daemon refuses, with a message
// meant for a person (it ends up in the agent's tool result).
type UserError struct{ Msg string }

func (e *UserError) Error() string { return e.Msg }

func userErr(format string, args ...any) error { return &UserError{Msg: fmt.Sprintf(format, args...)} }

var (
	idPattern       = regexp.MustCompile(`^[A-Za-z0-9_-]{8,64}$`)
	usernamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,64}$`)
)

// Manifest describes one backup's ciphertext chunks. It is stored in the
// clear on the peer, so it deliberately carries no names, paths or file
// counts -- only sizes and hashes of opaque chunks.
type Manifest struct {
	Version          int      `json:"version"`
	BackupID         string   `json:"backup_id"`
	CreatedAt        string   `json:"created_at"`
	TotalBytes       int64    `json:"total_bytes"`
	ChunkSHA256      []string `json:"chunk_sha256"`
	SigningPublicKey string   `json:"signing_public_key"`
}

// signedManifest is how a manifest travels and rests: the exact JSON bytes
// that were signed (never re-serialized, so the signature can't be broken
// by a field-order change) plus the signature, both base64.
type signedManifest struct {
	ManifestB64  string `json:"manifest"`
	SignatureB64 string `json:"signature"`
}

func signManifest(m Manifest, key ed25519.PrivateKey) (signedManifest, error) {
	raw, err := json.Marshal(m)
	if err != nil {
		return signedManifest{}, err
	}
	return signedManifest{
		ManifestB64:  base64.StdEncoding.EncodeToString(raw),
		SignatureB64: base64.StdEncoding.EncodeToString(ed25519.Sign(key, raw)),
	}, nil
}

// verifyManifest checks sm's signature against any of trustedKeys (base64
// ed25519 public keys) and that its embedded signing key is one of them.
func verifyManifest(sm signedManifest, trustedKeys []string) (Manifest, error) {
	raw, err := base64.StdEncoding.DecodeString(sm.ManifestB64)
	if err != nil {
		return Manifest{}, userErr("malformed manifest")
	}
	sig, err := base64.StdEncoding.DecodeString(sm.SignatureB64)
	if err != nil {
		return Manifest{}, userErr("malformed manifest signature")
	}
	var m Manifest
	if err := json.Unmarshal(raw, &m); err != nil {
		return Manifest{}, userErr("malformed manifest")
	}
	for _, k := range trustedKeys {
		if k != m.SigningPublicKey {
			continue
		}
		pub, err := base64.StdEncoding.DecodeString(k)
		if err != nil || len(pub) != ed25519.PublicKeySize {
			continue
		}
		if ed25519.Verify(ed25519.PublicKey(pub), raw, sig) {
			return m, nil
		}
	}
	return Manifest{}, userErr("manifest signature doesn't match any key registered to its sender")
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func chunkName(i int) string { return fmt.Sprintf("chunk-%05d", i) }

// Grant is one Backup Peer grant on this host, as casper_service reports it
// (GET /hosts/grants). The daemon decides every peer-side request from
// these; casper_service never decides for it.
type Grant struct {
	Grantee     string     `json:"grantee"`
	SigningKeys []string   `json:"signing_keys"`
	QuotaBytes  int64      `json:"quota_bytes"`
	WriteTier   string     `json:"write_tier"` // "allow" | "ask" | "deny"
	RevokedAt   *time.Time `json:"revoked_at,omitempty"`
}

// Store is one paired identity's backup state: its keys (as an owner) and
// the blobs others store with it (as a peer). Everything lives under root,
// which is per identity (see cmd/casper/daemon.go) -- two accounts paired to
// the same machine never see each other's backups either way.
type Store struct {
	root        string
	keyStore    KeyStore
	keyAccount  string
	fetchGrants func() ([]Grant, error)
	now         func() time.Time

	mu       sync.Mutex
	keys     *Keys
	grants   []Grant
	grantsAt time.Time
	prepares map[string]*prepareState
}

func NewStore(root string, ks KeyStore, keyAccount string, fetchGrants func() ([]Grant, error)) *Store {
	return &Store{
		root:        root,
		keyStore:    ks,
		keyAccount:  keyAccount,
		fetchGrants: fetchGrants,
		now:         time.Now,
		prepares:    map[string]*prepareState{},
	}
}

// Keys loads (or on first use creates) this identity's keys. storage is
// non-empty only when they were just created (see LoadOrCreateKeys).
func (s *Store) Keys() (keys *Keys, storage string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.keys != nil {
		return s.keys, "", nil
	}
	keys, storage, err = LoadOrCreateKeys(s.keyStore, s.keyAccount)
	if err != nil {
		return nil, "", err
	}
	s.keys = keys
	return keys, storage, nil
}

func (s *Store) dir(parts ...string) string {
	return filepath.Join(append([]string{s.root}, parts...)...)
}

func writeJSONFile(path string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func readJSONFile(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

func checkID(id string) error {
	if !idPattern.MatchString(id) {
		return userErr("invalid backup id")
	}
	return nil
}

var errNotFound = errors.New("not found")

func jsonUnmarshal(data []byte, v any) error { return json.Unmarshal(data, v) }
