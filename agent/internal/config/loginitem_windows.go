//go:build windows

// Login-item helpers make Casper start when the person signs in: a value under
// HKCU\...\CurrentVersion\Run (per user, no admin). Settings > Apps > Startup
// can turn it off without removing it (a flag under Explorer\StartupApproved\Run),
// so IsLoginItem reads that too and the menu checkbox tells the truth.
package config

import (
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/windows/registry"
)

const (
	runKey      = `Software\Microsoft\Windows\CurrentVersion\Run`
	approvedKey = `Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run`
)

// runValueName tells dev, test and prod builds apart ("Casper", "Casper-dev").
func runValueName() string { return appConfigSubdir() }

func runCommand() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return `"` + exe + `"`, nil
}

func loginItemRegistered(name string) (cmd string, ok bool) {
	k, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE)
	if err != nil {
		return "", false
	}
	defer k.Close()
	v, _, err := k.GetStringValue(name)
	return v, err == nil
}

// loginItemDisabled: StartupApproved holds 12 bytes per entry; an odd first
// byte (03) means the person turned it off in Settings.
func loginItemDisabled(name string) bool {
	k, err := registry.OpenKey(registry.CURRENT_USER, approvedKey, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	defer k.Close()
	b, _, err := k.GetBinaryValue(name)
	return err == nil && len(b) > 0 && b[0]&1 == 1
}

func isLoginItem(name string) bool {
	_, ok := loginItemRegistered(name)
	return ok && !loginItemDisabled(name)
}

func addLoginItem(name, cmd string) error {
	k, _, err := registry.CreateKey(registry.CURRENT_USER, runKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	if err := k.SetStringValue(name, cmd); err != nil {
		return err
	}
	if a, err := registry.OpenKey(registry.CURRENT_USER, approvedKey, registry.SET_VALUE); err == nil {
		_ = a.DeleteValue(name) // choosing it again overrides an earlier "off" in Settings
		a.Close()
	}
	return nil
}

func removeLoginItem(name string) error {
	for _, path := range []string{runKey, approvedKey} {
		k, err := registry.OpenKey(registry.CURRENT_USER, path, registry.SET_VALUE)
		if err != nil {
			continue
		}
		if err := k.DeleteValue(name); err != nil && err != registry.ErrNotExist {
			k.Close()
			return err
		}
		k.Close()
	}
	return nil
}

// refreshLoginItem points an existing login item at this copy of Casper if the
// app has moved. It leaves the person's Settings choice alone.
func refreshLoginItem(name, cmd string) error {
	cur, ok := loginItemRegistered(name)
	if !ok || strings.EqualFold(cur, cmd) {
		return nil
	}
	k, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	return k.SetStringValue(name, cmd)
}

func IsLoginItem() (bool, error) { return isLoginItem(runValueName()), nil }

func AddLoginItem() error {
	cmd, err := runCommand()
	if err != nil {
		return err
	}
	return addLoginItem(runValueName(), cmd)
}

func RefreshLoginItem() error {
	cmd, err := runCommand()
	if err != nil {
		return err
	}
	return refreshLoginItem(runValueName(), cmd)
}

func RemoveLoginItem() error { return removeLoginItem(runValueName()) }
