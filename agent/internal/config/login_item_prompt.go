package config

import (
	"os"
	"path/filepath"
)

func loginItemPromptDismissedPath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "login_item_prompt_dismissed.txt"), nil
}

// LoginItemPromptDismissed reports whether the user previously checked
// "Don't ask again" on the startup "add Casper to your login items?"
// prompt (see cmd/casper's promptAddToLoginItems) -- checked once at
// launch, before that prompt is shown at all.
func LoginItemPromptDismissed() bool {
	path, err := loginItemPromptDismissedPath()
	if err != nil {
		return false
	}
	_, err = os.Stat(path)
	return err == nil
}

// DismissLoginItemPrompt persists that the login-items prompt should never
// be shown again -- best-effort, matching SaveEnabled's posture (a write
// failure here just means the prompt reappears next launch, not worth
// failing anything over).
func DismissLoginItemPrompt() {
	path, err := loginItemPromptDismissedPath()
	if err != nil {
		return
	}
	_ = os.WriteFile(path, []byte("true"), 0o644)
}
