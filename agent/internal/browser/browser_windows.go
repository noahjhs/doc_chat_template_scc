//go:build windows

// Package browser opens a URL in the system default browser. Windows has no
// equivalent of the macOS --browser override (the Python version's own help
// text already documented "No effect on non-macOS" for that flag) -- sign-in
// itself works regardless.
package browser

import "os/exec"

func OpenURL(url, _ string) error {
	// The empty "" argument works around cmd.exe's `start` treating the
	// first quoted argument as the window title rather than the target --
	// a well-known, standard workaround.
	return exec.Command("cmd", "/c", "start", "", url).Run()
}
