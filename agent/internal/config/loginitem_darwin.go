//go:build darwin

// Login-item helpers make Casper start when the person logs in. They write a
// LaunchAgent (~/Library/LaunchAgents/<label>.plist) that opens the app, not
// a System Settings "Login Item" made through System Events: asking System
// Events needs the person's Automation approval, and macOS shows that question
// ("Casper wants access to control System Events") the moment the app
// launches, long before there's any context for it. A LaunchAgent needs no
// permission; macOS only shows a passing "background item added" notice.
package config

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// bundlePath resolves the running app bundle's own path (e.g.
// "/Applications/Casper.app") from the current executable's path
// (.../Casper.app/Contents/MacOS/Casper). Falls back to the raw executable
// path if it isn't running from inside a .app bundle (a bare dev build).
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

// launchAgentLabel tells dev and prod builds apart (they keep separate
// config directories, e.g. "Casper-dev").
func launchAgentLabel() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return "app." + strings.ToLower(filepath.Base(dir)) + ".login", nil
}

func launchAgentPath() (string, error) {
	label, err := launchAgentLabel()
	if err != nil {
		return "", err
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, "Library", "LaunchAgents", label+".plist"), nil
}

// IsLoginItem reports whether Casper is set to start at login.
func IsLoginItem() (bool, error) {
	path, err := launchAgentPath()
	if err != nil {
		return false, err
	}
	_, err = os.Stat(path)
	return err == nil, nil
}

// AddLoginItem makes Casper start at login. It refuses an app running from a
// macOS "App Translocation" path (what macOS runs a freshly downloaded app
// from until it's moved): that path is random and disappears at the next
// restart, so registering it would quietly break.
func AddLoginItem() error {
	app, err := bundlePath()
	if err != nil {
		return err
	}
	if strings.Contains(app, "/AppTranslocation/") {
		return fmt.Errorf("Casper is running from a temporary location; move it to Applications first")
	}
	label, err := launchAgentLabel()
	if err != nil {
		return err
	}
	path, err := launchAgentPath()
	if err != nil {
		return err
	}
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>%s</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
`, label, xmlEscape(app))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(plist), 0o644)
}

// RemoveLoginItem stops Casper starting at login.
func RemoveLoginItem() error {
	path, err := launchAgentPath()
	if err != nil {
		return err
	}
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	_ = exec.Command("launchctl", "bootout", fmt.Sprintf("gui/%d", os.Getuid()), path).Run() // best effort: not loaded yet is fine
	return nil
}

func xmlEscape(s string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(s)
}
