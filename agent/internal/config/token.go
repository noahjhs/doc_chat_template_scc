package config

import (
	"crypto/rand"
	"encoding/base64"
)

// randomToken generates n random bytes, URL-safe base64 encoded. Shared by
// routingkey.go (the persisted routing key) -- moved here from the old
// pairing package so it's usable without importing anything pairing-related.
func randomToken(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}
