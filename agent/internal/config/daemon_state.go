package config

import (
	"os"
	"path/filepath"
	"strings"
)

func enabledFilePath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "enabled.txt"), nil
}

// LoadEnabled returns the daemon's persisted on/off toggle state, defaulting
// to true (matching the pre-daemon behavior, where the tunnel was always up
// while the process ran) if never explicitly set.
func LoadEnabled() bool {
	path, err := enabledFilePath()
	if err != nil {
		return true
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return true
	}
	return strings.TrimSpace(string(data)) != "false"
}

// SaveEnabled persists the toggle state so it survives a daemon restart
// (reboot, login-item relaunch) -- best-effort, matching ClearSession's
// posture (a write failure here isn't worth failing the toggle over).
func SaveEnabled(enabled bool) {
	path, err := enabledFilePath()
	if err != nil {
		return
	}
	value := "true"
	if !enabled {
		value = "false"
	}
	_ = os.WriteFile(path, []byte(value), 0o644)
}

// ClearEnabled removes the persisted on/off toggle file entirely, so the
// next LoadEnabled call falls back to its own default (true) as if this
// installation had never been toggled before -- used by cmd/casper's
// --clear-preferences startup flag. Best-effort, same posture as
// SaveEnabled above.
func ClearEnabled() {
	path, err := enabledFilePath()
	if err != nil {
		return
	}
	_ = os.Remove(path)
}
