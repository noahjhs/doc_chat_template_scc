package config

import (
	"os"
	"path/filepath"
	"strings"
)

func routingKeyFilePath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "routing_key.txt"), nil
}

// LoadOrCreateRoutingKey returns this machine's stable relay routing key --
// generated once and persisted to routing_key.txt inside AppConfigDir(),
// reused across every future launch, sign-out, and re-pairing. Unlike a
// session token, this isn't a credential (it's just the path segment the
// relay uses to address this agent -- see relay/internal/registry), so it's
// deliberately independent of auth state: the daemon can hold a stable relay
// URL even while signed out, and reconnecting after a sign-out/sign-in cycle
// doesn't leave the web app needing to learn a new address.
func LoadOrCreateRoutingKey() (string, error) {
	path, err := routingKeyFilePath()
	if err != nil {
		return "", err
	}
	if data, readErr := os.ReadFile(path); readErr == nil {
		if key := strings.TrimSpace(string(data)); key != "" {
			return key, nil
		}
	}
	key, err := randomToken(24)
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(key), 0o600); err != nil {
		return "", err
	}
	_ = os.Chmod(path, 0o600)
	return key, nil
}
