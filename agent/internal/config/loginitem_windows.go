//go:build windows

// Login-item helpers have no real Windows implementation yet -- there's no
// packaged Windows build at all currently (see build/build_go_macos.sh),
// so this is a harmless stub rather than a compile break on any future
// Windows build: IsLoginItem always reports false, and Add/RemoveLoginItem
// are no-ops -- the tray menu's "Run on system startup" checkbox just stays
// permanently unchecked and does nothing when clicked.
package config

func IsLoginItem() (bool, error) { return false, nil }
func AddLoginItem() error        { return nil }
func RemoveLoginItem() error     { return nil }
