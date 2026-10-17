//go:build !windows

package winpkg

import "errors"

var errWindowsOnly = errors.New("the Windows installer only runs on Windows")

func Install(payload []byte, o Options) error { return errWindowsOnly }
func Uninstall(o Options) error               { return errWindowsOnly }

// RunUninstall is `Casper.exe uninstall`.
func RunUninstall(args []string) error { return errWindowsOnly }
