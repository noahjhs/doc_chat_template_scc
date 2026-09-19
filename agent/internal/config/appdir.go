package config

import (
	"os"
	"path/filepath"
)

// AppConfigDir is the stable per-user config directory -- holds session.json,
// the routing key, and the login-item prompt dismissal flag, independent of
// wherever the executable/app itself is installed or launched from (unlike
// casper_tool.py's old _app_dir-keyed scheme). os.UserConfigDir() gives the
// per-OS Application Support/AppData path directly. Created if missing.
func AppConfigDir() (string, error) {
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(base, "Casper")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}
