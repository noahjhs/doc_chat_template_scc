//go:build !windows

package setup

import (
	"os"
	"path/filepath"
	"strings"
)

// externalDrives lists what is mounted under /Volumes besides the startup disk.
func externalDrives() []string { return drivesUnder("/Volumes") }

func drivesUnder(volumes string) []string {
	var out []string
	entries, err := os.ReadDir(volumes)
	if err != nil {
		return nil
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		p := filepath.Join(volumes, e.Name())
		if r, err := filepath.EvalSymlinks(p); err == nil && r == "/" {
			continue // the startup disk
		}
		if fi, err := os.Stat(p); err == nil && fi.IsDir() {
			out = append(out, p)
		}
	}
	return out
}
