//go:build darwin

package config

import (
	"fmt"
	"os/exec"
	"strings"
)

// chooseWorkspaceFolder prompts via a native AppleScript folder-picker.
// "tell application System Events to activate" first for the same reason
// show_farewell_dialog() does: this process has no window/Dock presence of
// its own, so without activating a real, already-focusable application
// first, the dialog can surface behind whatever the user was last looking
// at instead of visibly grabbing attention.
func chooseWorkspaceFolder() (string, error) {
	script := `tell application "System Events" to activate
set chosenFolder to choose folder with prompt "Choose a folder for Casper to work in:"
return POSIX path of chosenFolder`
	out, err := exec.Command("osascript", "-e", script).Output()
	if err != nil {
		return "", fmt.Errorf("no workspace folder chosen")
	}
	path := strings.TrimSpace(string(out))
	if path == "" {
		return "", fmt.Errorf("no workspace folder chosen")
	}
	return path, nil
}
