// Package platform holds the small OS-specific actions the rest of the agent
// needs -- open a link, show a file or folder, find a well-known folder, free
// disk space -- so no caller shells out to `open` or a Statfs call itself.
package platform

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
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

// FileManager names the app that shows files: Finder or File Explorer.
func FileManager() string { return fileManagerName }

// RevealLabel is the button that opens the file manager on a file. A Windows
// dialog button is too narrow for "Show in File Explorer".
func RevealLabel() string { return revealLabel }

// Tray names where Casper's ghost sits on this OS, for messages that point at it.
func Tray() string { return trayName }

// KeyStore is what this OS calls the place Casper keeps passwords and keys.
func KeyStore() string { return keyStoreName }

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

// KillOnExit ties a started child process to this one: when Casper goes away
// by any route (crash, End task, logoff), Windows ends the child too. A no-op
// on a Mac, where the daemon stops Syncthing itself and launchd cleans up.
func KillOnExit(cmd *exec.Cmd) error { return killOnExit(cmd) }

// IsCloudOnly reports a file that exists only in the cloud (a OneDrive
// "files on demand" placeholder): reading it would download it. Always false
// off Windows.
func IsCloudOnly(path string) bool { return isCloudOnly(path) }

// ListensInbound says whether Casper's Syncthing may accept direct
// connections. On Windows it doesn't by default: a program that listens on
// the network makes Windows Firewall ask the person to allow it, and the
// pilot has no installer step to add a rule. Peers then reach it through
// Casper's relay (or it dials them). CASPER_LISTEN_INBOUND=1 overrides.
func ListensInbound() bool {
	if v := os.Getenv("CASPER_LISTEN_INBOUND"); v != "" {
		return v == "1"
	}
	return listensInboundByDefault
}

// HasCloudPlaceholders: this OS has files that exist only in the cloud, so
// folders are worth scanning for them.
func HasCloudPlaceholders() bool { return runtime.GOOS == "windows" }
