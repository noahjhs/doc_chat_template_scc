package mirror

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

var versionSuffix = regexp.MustCompile(`^(.*)~(\d{8}-\d{6})$`)

// PruneVersions trims a held mirror's history to the decided policy (every
// version for 7 days, then the newest per day up to 30 days, nothing older).
// It works on encrypted names: the policy only needs each version's
// timestamp and which file it belongs to, never its contents. Returns how
// many versions were removed.
func PruneVersions(folderPath string, now time.Time) int {
	root := filepath.Join(folderPath, ".stversions")
	byFile := map[string][]struct {
		path string
		at   time.Time
	}{}
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		m := versionSuffix.FindStringSubmatch(filepath.Base(p))
		if m == nil {
			return nil
		}
		at, err := time.ParseInLocation("20060102-150405", m[2], time.Local)
		if err != nil {
			return nil
		}
		key := filepath.Join(filepath.Dir(p), m[1])
		byFile[key] = append(byFile[key], struct {
			path string
			at   time.Time
		}{p, at})
		return nil
	})
	removed := 0
	for _, versions := range byFile {
		sort.Slice(versions, func(i, j int) bool { return versions[i].at.After(versions[j].at) })
		keptDay := map[string]bool{}
		for _, v := range versions {
			age := now.Sub(v.at)
			keep := false
			switch {
			case age <= 7*24*time.Hour:
				keep = true
			case age <= historyMaxAge:
				day := v.at.Format("2006-01-02")
				if !keptDay[day] {
					keptDay[day] = true
					keep = true
				}
			}
			if !keep && os.Remove(v.path) == nil {
				removed++
			}
		}
	}
	return removed
}

// VersionEntry is one stored version, by encrypted path and time.
type VersionEntry struct {
	EncryptedPath string    `json:"encrypted_path"`
	At            time.Time `json:"at"`
	Size          int64     `json:"size"`
}

// ListVersions lists a held folder's stored versions (encrypted names).
func ListVersions(folderPath string) []VersionEntry {
	root := filepath.Join(folderPath, ".stversions")
	var out []VersionEntry
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		m := versionSuffix.FindStringSubmatch(filepath.Base(p))
		if m == nil {
			return nil
		}
		at, err := time.ParseInLocation("20060102-150405", m[2], time.Local)
		if err != nil {
			return nil
		}
		rel, _ := filepath.Rel(root, filepath.Join(filepath.Dir(p), m[1]))
		info, _ := d.Info()
		var size int64
		if info != nil {
			size = info.Size()
		}
		out = append(out, VersionEntry{EncryptedPath: filepath.ToSlash(rel), At: at, Size: size})
		return nil
	})
	sort.Slice(out, func(i, j int) bool {
		if out[i].EncryptedPath != out[j].EncryptedPath {
			return out[i].EncryptedPath < out[j].EncryptedPath
		}
		return out[i].At.After(out[j].At)
	})
	return out
}

// VersionFile is where a listed version lives on disk.
func VersionFile(folderPath, encryptedPath string, at time.Time) string {
	clean := filepath.Clean("/" + filepath.FromSlash(encryptedPath))
	if strings.Contains(clean, "..") {
		return ""
	}
	return filepath.Join(folderPath, ".stversions", clean[1:]) + "~" + at.Format("20060102-150405")
}
