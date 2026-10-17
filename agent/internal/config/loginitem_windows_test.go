//go:build windows

package config

import (
	"testing"

	"golang.org/x/sys/windows/registry"
)

func TestLoginItemRoundTripAndSettingsToggle(t *testing.T) {
	const name = "Casper-unittest"
	defer removeLoginItem(name)
	if isLoginItem(name) {
		t.Fatal("should start absent")
	}
	if err := addLoginItem(name, `"C:\A\Casper.exe"`); err != nil {
		t.Fatal(err)
	}
	if !isLoginItem(name) {
		t.Fatal("added, so it should be a login item")
	}
	// Settings > Startup turning it off writes 03 00 00 00 + a FILETIME.
	k, _, err := registry.CreateKey(registry.CURRENT_USER, approvedKey, registry.SET_VALUE)
	if err != nil {
		t.Fatal(err)
	}
	if err := k.SetBinaryValue(name, []byte{3, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8}); err != nil {
		t.Fatal(err)
	}
	k.Close()
	if isLoginItem(name) {
		t.Fatal("turned off in Settings, so the checkbox must be off")
	}
	if err := refreshLoginItem(name, `"D:\B\Casper.exe"`); err != nil {
		t.Fatal(err)
	}
	if cur, _ := loginItemRegistered(name); cur != `"D:\B\Casper.exe"` {
		t.Fatalf("refresh after a move: %s", cur)
	}
	if isLoginItem(name) {
		t.Fatal("refresh must not undo the person's choice")
	}
	if err := addLoginItem(name, `"D:\B\Casper.exe"`); err != nil || !isLoginItem(name) {
		t.Fatalf("choosing it again should turn it on: %v", err)
	}
	if err := removeLoginItem(name); err != nil || isLoginItem(name) {
		t.Fatalf("remove: %v", err)
	}
	if err := removeLoginItem(name); err != nil {
		t.Fatalf("removing twice should be fine: %v", err)
	}
}
