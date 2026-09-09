//go:build darwin

// Package activate re-foregrounds the user's browser after a casper://pair
// hand-off completes. Exists because a purely browser-side fix didn't hold
// up: the signed-in tab calling window.focus() from its own JS (see
// pages/signin.py) is a best-effort attempt at best, since modern browsers
// restrict script-triggered focus changes that aren't tied to a fresh,
// direct user gesture -- and by the time this daemon has finished
// processing the pairing event, the tab's redirect script is running well
// outside that window. This sidesteps the whole question by using a
// mechanism already proven reliable in this codebase for OS-level app
// activation (see internal/dialog's farewell dialog, which uses the same
// "tell application ... to activate" AppleScript pattern) instead of
// fighting browser focus-stealing restrictions from an unprivileged script.
package activate

import (
	"encoding/json"
	"os"
	"os/exec"
)

var bundleIDToAppName = map[string]string{
	"com.apple.safari":      "Safari",
	"com.google.chrome":     "Google Chrome",
	"org.mozilla.firefox":   "Firefox",
	"com.microsoft.edgemac": "Microsoft Edge",
}

// defaultBrowserAppName reads the system's actual configured default
// browser's application name from LaunchServices' plist, or "" if it can't
// be determined. Mirrors the same lookup the old (now-deleted) browser
// package used to open a pairing tab with, before the daemon conversion --
// resurrected here for the opposite direction (re-focusing, not opening).
func defaultBrowserAppName() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	plistPath := home + "/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
	out, err := exec.Command("plutil", "-convert", "json", "-o", "-", plistPath).Output()
	if err != nil {
		return ""
	}
	var data struct {
		LSHandlers []struct {
			LSHandlerURLScheme string `json:"LSHandlerURLScheme"`
			LSHandlerRoleAll   string `json:"LSHandlerRoleAll"`
		} `json:"LSHandlers"`
	}
	if err := json.Unmarshal(out, &data); err != nil {
		return ""
	}
	for _, h := range data.LSHandlers {
		if h.LSHandlerURLScheme == "http" {
			return bundleIDToAppName[h.LSHandlerRoleAll]
		}
	}
	return ""
}

// DefaultBrowser brings the system default browser to the foreground.
// Best-effort and silent on failure -- there's no fallback left if this
// doesn't work, and it should never be allowed to disrupt pairing itself.
// A real limitation, not just an implementation gap: this assumes the user
// signed in using their default browser, which isn't always true (no
// signal is available to know which browser actually has the relevant
// tab), so it can end up activating the wrong application if not.
func DefaultBrowser() {
	appName := defaultBrowserAppName()
	if appName == "" {
		return
	}
	// appName only ever comes from the fixed map above, never from
	// arbitrary/external input, so no AppleScript-string escaping is needed
	// here (unlike internal/dialog's messages, which do carry arbitrary text).
	_ = exec.Command("osascript", "-e", `tell application "`+appName+`" to activate`).Run()
}
