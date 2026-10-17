package mirror

import (
	"io/fs"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	cloudScanEvery = 10 * time.Minute
	cloudScanMax   = 500
	cloudScanLimit = 20 * time.Second
)

type cloudScan struct {
	at    time.Time
	names []string
}

// cloudOnlyFiles lists (up to cloudScanMax, slash-separated, relative) the
// files in a folder that live only in the cloud -- OneDrive "files on demand"
// placeholders. The result is cached for a few minutes: walking a large folder
// on every reconcile would be slow. Always empty off Windows.
func (m *Manager) cloudOnlyFiles(folderID, root string) []string {
	if root == "" || m.cloudCheck == nil {
		return nil
	}
	m.mu.Lock()
	if m.cloud == nil {
		m.cloud = map[string]cloudScan{}
	}
	c, ok := m.cloud[folderID]
	m.mu.Unlock()
	if ok && time.Since(c.at) < cloudScanEvery {
		return c.names
	}
	names := scanCloudOnly(root, m.cloudCheck)
	m.mu.Lock()
	m.cloud[folderID] = cloudScan{at: time.Now(), names: names}
	m.mu.Unlock()
	return names
}

func scanCloudOnly(root string, isCloud func(string) bool) []string {
	var out []string
	deadline := time.Now().Add(cloudScanLimit)
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if len(out) >= cloudScanMax || time.Now().After(deadline) {
			return fs.SkipAll
		}
		if d.IsDir() {
			if n := d.Name(); n == ".stversions" || n == ".stfolder" {
				return fs.SkipDir
			}
			return nil
		}
		if isCloud(p) {
			if rel, err := filepath.Rel(root, p); err == nil {
				out = append(out, filepath.ToSlash(rel))
			}
		}
		return nil
	})
	return out
}

// ignoreLine is the Syncthing ignore pattern for exactly one file: anchored
// to the folder, with the characters patterns treat specially made literal.
// Syncthing on Windows can't take a backslash escape (it is the path
// separator there), so the characters go in a one-character class instead (a "]" can't, so it becomes "?").
func ignoreLine(rel string) string { return ignoreLineFor(rel, runtime.GOOS == "windows") }

func ignoreLineFor(rel string, windows bool) string {
	var b strings.Builder
	b.WriteByte('/')
	for _, r := range rel {
		switch {
		case !strings.ContainsRune(`\*?[]{}!#`, r):
			b.WriteRune(r)
		case windows && r == ']': // a class can't hold it: any one character
			b.WriteByte('?')
		case windows && strings.ContainsRune(`*?[{}`, r):
			b.WriteString("[" + string(r) + "]")
		case windows: // ! and # only mean something at the start of a line
			b.WriteRune(r)
		default:
			b.WriteByte('\\')
			b.WriteRune(r)
		}
	}
	return b.String()
}
