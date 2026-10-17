//go:build !windows

package mirror

import (
	"os"
	"testing"
)

// makeUnreadable writes a file Syncthing can't read (no permissions).
func makeUnreadable(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, []byte("no"), 0o000); err != nil {
		t.Fatal(err)
	}
}
