// Package platform holds the small OS-specific actions the rest of the agent
// needs -- open a link, show a file or folder, find a well-known folder, free
// disk space -- so no caller shells out to `open` or a Statfs call itself.
package platform

import (
	"os"
	"os/exec"
	"path/filepath"
)

// OpenURL opens a link (or a file) with the default handler.
func OpenURL(u string) error { return openURL(u) }

// Reveal shows a file selected in the file manager (Finder, Explorer).
func Reveal(path string) error { return reveal(path) }

// OpenFolder opens a folder in the file manager.
func OpenFolder(dir string) error { return openFolder(dir) }

// HideWindow keeps a child process from opening a console window (Windows;
// no-op elsewhere).
func HideWindow(cmd *exec.Cmd) { hideWindow(cmd) }

// KnownFolder is the folder the person sees as Documents, Desktop, Downloads,
// Pictures, Music or Movies/Videos. On a Mac it's under home; on Windows it
// asks the shell, since OneDrive often moves them. Falls back to home/<name>.
func KnownFolder(name string) string {
	if dir := knownFolder(name); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, name)
}

// FreeBytes is the space a person can still use on the volume holding path
// (or its nearest existing parent).
func FreeBytes(path string) int64 {
	for p := path; ; {
		if n, ok := freeBytes(p); ok {
			return n
		}
		parent := filepath.Dir(p)
		if parent == p {
			return 0
		}
		p = parent
	}
}
