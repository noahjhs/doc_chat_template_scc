package mirror

import (
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// MaintainStaging makes an owned folder's staging area hold exactly its
// at-risk files (mirroring.md, "Layer 2: a catcher, holding only the gap").
// Files are hard-linked where possible, so staging usually costs no disk
// space; an editor that saves by replacing the file gets a fresh link.
// Anything no longer at risk is removed, and Syncthing propagates the
// removal to the catcher, which keeps no history.
func (m *Manager) MaintainStaging(f OwnedFolder, atRisk []string) error {
	stage := m.StagingPath(f.ID)
	if err := os.MkdirAll(stage, 0o700); err != nil {
		return err
	}
	want := map[string]bool{}
	for _, rel := range atRisk {
		want[filepath.FromSlash(rel)] = true
		src := filepath.Join(f.Path, filepath.FromSlash(rel))
		dst := filepath.Join(stage, filepath.FromSlash(rel))
		if sameFile(src, dst) {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
			return err
		}
		_ = os.Remove(dst)
		if err := os.Link(src, dst); err != nil {
			if err := copyFile(src, dst); err != nil {
				return err
			}
		}
	}
	// Remove what's no longer at risk, then any empty directories.
	var dirs []string
	_ = filepath.WalkDir(stage, func(p string, d fs.DirEntry, err error) error {
		if err != nil || p == stage {
			return nil
		}
		if d.IsDir() {
			if d.Name() == ".stfolder" {
				return filepath.SkipDir
			}
			dirs = append(dirs, p)
			return nil
		}
		rel, _ := filepath.Rel(stage, p)
		if !want[rel] {
			_ = os.Remove(p)
		}
		return nil
	})
	for i := len(dirs) - 1; i >= 0; i-- {
		_ = os.Remove(dirs[i]) // only succeeds if empty
	}
	return nil
}

func sameFile(a, b string) bool {
	ia, err := os.Stat(a)
	if err != nil {
		return false
	}
	ib, err := os.Stat(b)
	if err != nil {
		return false
	}
	sa, ok1 := ia.Sys().(*syscall.Stat_t)
	sb, ok2 := ib.Sys().(*syscall.Stat_t)
	if ok1 && ok2 {
		return sa.Dev == sb.Dev && sa.Ino == sb.Ino
	}
	return os.SameFile(ia, ib)
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// OverlayCatcher lays a restoring folder's catcher contents (the changes no
// mirror had when the original Mac was lost) over what the mirrors
// returned: a file is copied in if it's missing or the catcher's copy is
// newer. Returns how many files it applied. Deletions were never caught
// (mirroring.md), so nothing is removed.
func (m *Manager) OverlayCatcher(f OwnedFolder) (int, error) {
	stage := m.StagingPath(f.ID)
	applied := 0
	for _, rel := range listFiles(stage) {
		src := filepath.Join(stage, filepath.FromSlash(rel))
		dst := filepath.Join(f.Path, filepath.FromSlash(rel))
		si, err := os.Stat(src)
		if err != nil {
			continue
		}
		if di, err := os.Stat(dst); err == nil && !si.ModTime().After(di.ModTime()) {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return applied, err
		}
		if err := copyFile(src, dst); err != nil {
			return applied, err
		}
		_ = os.Chtimes(dst, si.ModTime(), si.ModTime())
		applied++
	}
	return applied, nil
}

const orphanMarker = ".casper-orphaned"

// CleanupOrphans deletes held folders (someone else's encrypted data) that
// casper_service no longer wants this Mac to hold -- mirroring stopped, or
// the grant was revoked -- once they've been unwanted for `grace` (7 days:
// the owner can still restore from them meanwhile). A folder that becomes
// wanted again is un-marked. Returns the IDs it deleted.
func (m *Manager) CleanupOrphans(wanted map[string]bool, grace time.Duration, now time.Time) []string {
	root := filepath.Join(m.dataDir, "held")
	entries, _ := os.ReadDir(root)
	var deleted []string
	for _, e := range entries {
		if !e.IsDir() || e.Name() == "restore-tmp" || !strings.HasPrefix(e.Name(), FolderPrefix) {
			continue
		}
		dir := filepath.Join(root, e.Name())
		marker := filepath.Join(dir, orphanMarker)
		if wanted[e.Name()] {
			_ = os.Remove(marker)
			continue
		}
		info, err := os.Stat(marker)
		if err != nil {
			_ = os.WriteFile(marker, []byte(now.UTC().Format(time.RFC3339)), 0o600)
			continue
		}
		if now.Sub(info.ModTime()) >= grace {
			if os.RemoveAll(dir) == nil {
				deleted = append(deleted, e.Name())
			}
		}
	}
	return deleted
}
