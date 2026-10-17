package platform

import (
	"path/filepath"
	"testing"
)

func TestFreeBytesWalksUpToExistingParent(t *testing.T) {
	if FreeBytes(filepath.Join(t.TempDir(), "not", "there", "yet")) <= 0 {
		t.Fatal("expected free space for a path that doesn't exist yet")
	}
}

func TestKnownFolderFallsBackToHome(t *testing.T) {
	if KnownFolder("Documents") == "" || KnownFolder("Nonsense") == "" {
		t.Fatal("empty known folder")
	}
}
