//go:build !windows

package main

// The Windows shell pieces (see shell_windows.go) have nothing to do on a Mac:
// LaunchServices keeps one Casper running and hands it every casper:// link,
// and the menu bar needs no icon swap or "where is Casper" hint.

func claimInstance(deliver func(rawURL string)) bool { return true }

func releaseInstance() {}

func applyTrayIcon(enabled bool) {}

func showTrayHint(force bool) {}

func attachConsole() {}
