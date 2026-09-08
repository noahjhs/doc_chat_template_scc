package config

import "testing"

func TestLoadEnabled_DefaultsTrue(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)

	if !LoadEnabled() {
		t.Fatal("expected LoadEnabled to default to true when never explicitly set")
	}
}

func TestSaveEnabled_RoundTrip(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOME", dir)

	SaveEnabled(false)
	if LoadEnabled() {
		t.Fatal("expected LoadEnabled to report false after SaveEnabled(false)")
	}

	SaveEnabled(true)
	if !LoadEnabled() {
		t.Fatal("expected LoadEnabled to report true after SaveEnabled(true)")
	}
}
