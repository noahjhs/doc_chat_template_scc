package config

import (
	"os"
	"path/filepath"
	"runtime"
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
	for _, env := range []string{"dev", "test"} { // test: the end-to-end test deployment
		if strings.HasPrefix(domain, env+"-") {
			return "Casper-" + env
		}
	}
	return "Casper"
}

// ProductName is what people see for this build: "Casper", "Casper Dev" or
// "Casper Test" (the install folder, the Start menu entry, Apps & features).
func ProductName() string {
	name, env, found := strings.Cut(appConfigSubdir(), "-")
	if !found {
		return name
	}
	return name + " " + strings.ToUpper(env[:1]) + env[1:]
}

// configBase is where the per-user app directories live: the per-OS config
// directory, except on Windows, where os.UserConfigDir is %APPDATA% (Roaming)
// and the Syncthing index database and logs must not roam, so it is
// %LOCALAPPDATA% (os.UserCacheDir).
func configBase() (string, error) {
	if runtime.GOOS == "windows" {
		return os.UserCacheDir()
	}
	return os.UserConfigDir()
}

// AppConfigDir is the stable per-user config directory -- holds session.json,
// the routing key, and the login-item prompt dismissal flag, independent of
// wherever the executable/app itself is installed or launched from (unlike
// casper_tool.py's old _app_dir-keyed scheme). os.UserConfigDir() gives the
// per-OS Application Support/AppData path directly. Created if missing.
func AppConfigDir() (string, error) {
	base, err := configBase()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(base, appConfigSubdir())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}

// PairURLScheme is the custom URL scheme this build's Casper.app registers
// for pairing hand-offs -- "casper-dev"/"casper-test" for dev/test builds, "casper" for prod
// (see build/build_go_macos.sh's URL_SCHEME, derived from the same dev-
// prefix convention as appConfigSubdir).
func PairURLScheme() string {
	return strings.ToLower(appConfigSubdir())
}
