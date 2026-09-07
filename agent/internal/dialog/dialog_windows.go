//go:build windows

// Package dialog shows the native macOS "farewell" modal -- a no-op on
// Windows, mirroring casper_tool.py's `if sys.platform != "darwin": return`.
package dialog

func ShowFarewellDialog(_ func(format string, args ...any)) {}
