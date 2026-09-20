package config

import (
	"os"
	"path/filepath"
	"strings"
)

// appConfigSubdir returns "Casper" for a prod build, "Casper-dev" for a dev
// one -- derived from the embedded auth domain's own "dev-" prefix (the
// same convention this whole project already uses to tell dev/prod apart,
// e.g. scripts/smoke_test.sh's own AUTH_DOMAIN defaults), rather than a
// separate embedded marker, so there's exactly one source of truth for
// which environment a given build was compiled for. This is what makes a
// dev-build and prod-build Casper.app safe to run side by side on the SAME
// machine/user: without it, both would read/write the exact identical
// ~/Library/Application Support/Casper (session.json, routing_key.txt),
// silently clobbering each other's pairing state the moment either wrote
// to it -- confirmed directly by inspection, not just theorized, before
// this existed. CONTROL_TOOL_AUTH_DOMAIN (a local-dev/CI override, see
// domain.go's LoadAuthDomain) is honored here too, so pointing a locally
// run binary at a dev-prefixed domain gets the same isolated directory a
// real dev build would.
func appConfigSubdir() string {
	domain := strings.TrimSpace(embeddedAuthServer)
	if v := os.Getenv("CONTROL_TOOL_AUTH_DOMAIN"); v != "" {
		domain = v
	}
	if strings.HasPrefix(domain, "dev-") {
		return "Casper-dev"
	}
	return "Casper"
}

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
	dir := filepath.Join(base, appConfigSubdir())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}
