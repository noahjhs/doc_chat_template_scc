//go:build darwin

// Login-item helpers register/unregister Casper.app as a macOS Login Item
// (System Settings > General > Login Items), via System Events over
// osascript -- the same automation approach internal/dialog uses for native
// modals. There's no cgo-free public API for this, and pulling in the
// ServiceManagement framework (SMAppService) would mean a second, heavier
// cgo surface just for this one feature.
package config

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// loginItemName matches Info.plist's CFBundleName (build/build_go_macos.sh)
// -- System Events derives a login item's own "name" from the app bundle's
// display name when it's added, so this is what IsLoginItem/RemoveLoginItem
// look for/target, rather than a path (which wouldn't survive the bundle
// being moved after being registered).
const loginItemName = "Casper"

// bundlePath resolves the running app bundle's own path (e.g.
// "/Applications/Casper.app") from the current executable's path
// (.../Casper.app/Contents/MacOS/Casper) -- works no matter where the user
// actually placed the bundle (Applications, Desktop, Downloads, ...). Falls
// back to the raw executable path if it isn't running from inside a .app
// bundle at all (e.g. a bare dev build) -- login items can technically
// still point at a plain executable, just without any of the bundle's own
// icon/Info.plist behavior.
func bundlePath() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	const marker = ".app/Contents/MacOS/"
	if idx := strings.Index(exe, marker); idx != -1 {
		return exe[:idx+len(".app")], nil
	}
	return exe, nil
}

func escapeForAppleScript(s string) string {
	r := strings.NewReplacer(`\`, `\\`, `"`, `\"`)
	return r.Replace(s)
}

// IsLoginItem reports whether Casper is currently registered as a macOS
// Login Item, matched by name (not path) -- so it still recognizes itself
// as already-registered even if the bundle has since been moved.
func IsLoginItem() (bool, error) {
	out, err := exec.Command("osascript", "-e",
		`tell application "System Events" to get the name of every login item`,
	).Output()
	if err != nil {
		return false, fmt.Errorf("couldn't list login items: %w", err)
	}
	for _, name := range strings.Split(string(out), ",") {
		if strings.TrimSpace(name) == loginItemName {
			return true, nil
		}
	}
	return false, nil
}

// AddLoginItem registers the running app bundle as a macOS Login Item. Not
// idempotent at the OS level (re-running this while already registered adds
// a second, functionally-redundant entry with the same name) -- callers
// should check IsLoginItem first; both call sites in cmd/casper do.
func AddLoginItem() error {
	path, err := bundlePath()
	if err != nil {
		return err
	}
	script := fmt.Sprintf(
		`tell application "System Events" to make login item at end with properties {path:"%s", hidden:false, name:"%s"}`,
		escapeForAppleScript(path), escapeForAppleScript(loginItemName),
	)
	if out, err := exec.Command("osascript", "-e", script).CombinedOutput(); err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// RemoveLoginItem unregisters Casper as a macOS Login Item, by name.
func RemoveLoginItem() error {
	script := fmt.Sprintf(`tell application "System Events" to delete login item "%s"`, escapeForAppleScript(loginItemName))
	if out, err := exec.Command("osascript", "-e", script).CombinedOutput(); err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}
