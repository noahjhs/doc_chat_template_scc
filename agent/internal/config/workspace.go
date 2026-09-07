package config

import (
	"os"
	"path/filepath"
	"strings"
)

// AppConfigDir is the stable per-user config directory -- holds session.json
// and the remembered workspace pointer, independent of wherever the
// executable/app itself is installed or launched from (unlike casper_tool.py's
// old _app_dir-keyed scheme). os.UserConfigDir() gives the per-OS Application
// Support/AppData path directly. Created if missing.
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

func workspaceFilePath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "workspace.txt"), nil
}

// ResolveWorkspaceDir is the confined workspace root. Checks, in order:
// CONTROL_TOOL_WORKSPACE (env override -- for tests/CI/dev, so this never
// blocks on a GUI dialog in a non-interactive run), then the remembered
// pointer in workspace.txt inside AppConfigDir(), then prompts via a native
// folder-picker dialog if neither is set or the remembered folder no longer
// exists -- persisting whatever's chosen back to workspace.txt so later runs
// are silent.
//
// Importing this package at all risks triggering that dialog the moment
// ResolveWorkspaceDir is called -- callers in a non-interactive context
// (tests, CI) must set CONTROL_TOOL_WORKSPACE first.
func ResolveWorkspaceDir() (string, error) {
	if v := os.Getenv("CONTROL_TOOL_WORKSPACE"); v != "" {
		if resolved, err := filepath.EvalSymlinks(v); err == nil {
			return resolved, nil
		}
		return v, nil
	}

	workspaceFile, err := workspaceFilePath()
	if err != nil {
		return "", err
	}
	if data, readErr := os.ReadFile(workspaceFile); readErr == nil {
		saved := strings.TrimSpace(string(data))
		if saved != "" {
			if info, statErr := os.Stat(saved); statErr == nil && info.IsDir() {
				if resolved, err := filepath.EvalSymlinks(saved); err == nil {
					return resolved, nil
				}
				return saved, nil
			}
		}
	}

	chosen, err := chooseWorkspaceFolder()
	if err != nil {
		return "", err
	}
	resolved, err := filepath.EvalSymlinks(chosen)
	if err != nil {
		resolved = chosen
	}
	if err := os.WriteFile(workspaceFile, []byte(resolved), 0o644); err != nil {
		return "", err
	}
	return resolved, nil
}
