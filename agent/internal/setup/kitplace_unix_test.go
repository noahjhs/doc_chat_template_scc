//go:build !windows

package setup

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDrivesUnderListsMountedVolumes(t *testing.T) {
	vols := t.TempDir()
	if got := drivesUnder(vols); len(got) != 0 {
		t.Fatalf("no drives: %v", got)
	}
	if err := os.Mkdir(filepath.Join(vols, "BACKUP"), 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.Mkdir(filepath.Join(vols, ".hidden"), 0o755)
	if got := drivesUnder(vols); len(got) != 1 || got[0] != filepath.Join(vols, "BACKUP") {
		t.Fatalf("with a drive: %v", got)
	}
}
