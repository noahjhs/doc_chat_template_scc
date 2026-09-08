//go:build darwin

// Package browser opens a URL in a specific or default browser, with a
// macOS-specific special case for a cold-launched Safari (see
// openInSafarisStartupTab). A direct port of casper_tool.py's
// open_url_with_browser()/get_default_browser_app_name()/_is_app_running()/
// _open_in_safaris_startup_tab().
package browser

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"time"
)

var bundleIDToAppName = map[string]string{
	"com.apple.safari":      "Safari",
	"com.google.chrome":     "Google Chrome",
	"org.mozilla.firefox":   "Firefox",
	"com.microsoft.edgemac": "Microsoft Edge",
}

// getDefaultBrowserAppName reads the system's actual configured default
// browser's application name (e.g. "Safari") from LaunchServices' plist, or
// "" if it can't be determined. Confirmed against a real machine that the
// "http" URL scheme handler's role is a bundle id (e.g. "com.google.chrome"),
// not assumed from documentation.
func getDefaultBrowserAppName() string {
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

func isAppRunning(appName string) bool {
	out, err := exec.Command("osascript", "-e", `application "`+appName+`" is running`).Output()
	return err == nil && strings.TrimSpace(string(out)) == "true"
}

// openInSafarisStartupTab launches Safari and navigates its own
// freshly-created startup tab (Start Page, or a restored session) directly
// to url, instead of opening a separate new tab alongside it -- a cold
// Safari launch otherwise leaves two tabs open. Only used for a cold launch
// (see isAppRunning) -- once Safari's already running, a plain new tab is
// the expected behavior. Returns true on success, false to fall back to the
// normal open path (e.g. Safari isn't installed, or never got a window
// within the timeout).
func openInSafarisStartupTab(url string) bool {
	if err := exec.Command("osascript", "-e", `tell application "Safari" to launch`).Run(); err != nil {
		return false
	}

	deadline := time.Now().Add(10 * time.Second)
	hasWindow := false
	for time.Now().Before(deadline) {
		out, err := exec.Command("osascript", "-e", `tell application "Safari" to count of windows`).Output()
		if err == nil {
			if n := strings.TrimSpace(string(out)); n != "" && n != "0" {
				hasWindow = true
				break
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	if !hasWindow {
		return false
	}

	script := `tell application "Safari" to set URL of document 1 to "` + url + `"`
	if err := exec.Command("osascript", "-e", script).Run(); err != nil {
		return false
	}
	_ = exec.Command("osascript", "-e", `tell application "Safari" to activate`).Run()
	return true
}

// OpenURL opens url in browserName (a macOS application name) if given,
// otherwise the system default. Special-cased for a cold-launched Safari
// specifically, whether that's because browserName says so explicitly, or
// (the common case) no override was given and the system's actual default
// turns out to be Safari.
func OpenURL(url, browserName string) error {
	resolvedName := browserName
	if resolvedName == "" {
		resolvedName = getDefaultBrowserAppName()
	}
	if strings.EqualFold(strings.TrimSpace(resolvedName), "safari") && !isAppRunning("Safari") {
		if openInSafarisStartupTab(url) {
			return nil
		}
	}
	if browserName != "" {
		return exec.Command("open", "-a", browserName, url).Run()
	}
	return exec.Command("open", url).Run()
}
