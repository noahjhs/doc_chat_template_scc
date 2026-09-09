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

// LoadWorkspaceDirs returns the persisted set of confined directories --
// one per line in workspace.txt, possibly empty (no file yet, or every
// remembered entry has since been deleted/unmounted). Unlike the old
// ResolveWorkspaceDir, this never blocks on a native dialog and never
// writes anything -- the daemon now starts with whatever's already been
// added, even zero directories, rather than requiring a folder choice
// before it can do anything at all (see AddWorkspaceDir for how a
// directory actually gets added, on demand from the web app's "+"
// button). A line whose directory no longer exists is silently dropped,
// the same "remembered folder no longer exists" fallback the old
// single-directory version had, just per-entry instead of all-or-nothing.
func LoadWorkspaceDirs() ([]string, error) {
	workspaceFile, err := workspaceFilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(workspaceFile)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var dirs []string
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		info, statErr := os.Stat(line)
		if statErr != nil || !info.IsDir() {
			continue
		}
		if resolved, err := filepath.EvalSymlinks(line); err == nil {
			dirs = append(dirs, resolved)
		} else {
			dirs = append(dirs, line)
		}
	}
	return dirs, nil
}

// saveWorkspaceDirs overwrites workspace.txt with exactly dirs, one per
// line.
func saveWorkspaceDirs(dirs []string) error {
	workspaceFile, err := workspaceFilePath()
	if err != nil {
		return err
	}
	return os.WriteFile(workspaceFile, []byte(strings.Join(dirs, "\n")), 0o644)
}

// AddWorkspaceDir prompts via a native folder-picker dialog and, if the
// user actually chose a folder (rather than cancelling), persists it
// alongside whatever directories are already remembered and returns its
// resolved path. Returns ("", nil) -- not an error -- on cancellation, so
// callers can tell "nothing to add" apart from a real failure. Blocks for
// as long as the dialog is on screen; callers on a request-handling path
// with a timeout (see commands.Handler.runAddDirectory) must run this in
// a goroutine, not inline.
func AddWorkspaceDir() (string, error) {
	chosen, err := chooseWorkspaceFolder()
	if err != nil {
		return "", nil
	}
	resolved, err := filepath.EvalSymlinks(chosen)
	if err != nil {
		resolved = chosen
	}
	existing, err := LoadWorkspaceDirs()
	if err != nil {
		return "", err
	}
	for _, d := range existing {
		if d == resolved {
			return resolved, nil
		}
	}
	if err := saveWorkspaceDirs(append(existing, resolved)); err != nil {
		return "", err
	}
	return resolved, nil
}
