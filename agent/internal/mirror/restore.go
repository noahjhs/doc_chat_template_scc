package mirror

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
)

// Restore layout (docs/product/restore-design.md, section 4): a restore is a
// copy next to the original with a dated name, unless the person chose
// replace.

const (
	ModeCopy    = "copy"
	ModeReplace = "replace"

	// FitMargin is kept free after a restore.
	FitMargin int64 = 256 << 20
)

// safeRel cleans a folder-relative path from a decrypted trailer and refuses
// anything that would leave the folder.
func safeRel(rel string) (string, error) {
	clean := filepath.Clean("/" + filepath.FromSlash(rel))[1:]
	if clean == "" || clean == "." || strings.HasPrefix(clean, "..") {
		return "", errors.New("bad file path")
	}
	return clean, nil
}

// Within reports whether path is root or inside it.
func Within(root, path string) bool {
	if runtime.GOOS == "windows" { // case-insensitive file names
		root, path = strings.ToLower(root), strings.ToLower(path)
	}
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// DatedFileName is "Budget.xlsx" at 2026-10-14 09:12 -> "Budget (as of
// 2026-10-14 09.12).xlsx" (no colons: Finder shows them as slashes).
func DatedFileName(name string, t time.Time) string {
	ext := filepath.Ext(name)
	stem := strings.TrimSuffix(name, ext)
	if stem == "" { // a dotfile: ".env" is all stem
		stem, ext = name, ""
	}
	return fmt.Sprintf("%s (as of %s)%s", stem, t.Format("2006-01-02 15.04"), ext)
}

// uniquePath returns p, or p with " 2", " 3"... added before the extension
// if something is already there.
func uniquePath(p string) string {
	if _, err := os.Lstat(p); err != nil {
		return p
	}
	ext := filepath.Ext(p)
	stem := strings.TrimSuffix(p, ext)
	for i := 2; ; i++ {
		q := fmt.Sprintf("%s %d%s", stem, i, ext)
		if _, err := os.Lstat(q); err != nil {
			return q
		}
	}
}

// FolderCopyRoot is where a whole-folder copy goes: a sibling of the
// mirrored folder, "Documents (as of 2026-10-10)"; in Casper Restores if the
// sibling would fall outside home (the folder is the home folder itself).
// Never an existing path.
func FolderCopyRoot(folderPath, home string, asOf time.Time) string {
	name := fmt.Sprintf("%s (as of %s)", filepath.Base(folderPath), asOf.Format("2006-01-02"))
	parent := filepath.Dir(folderPath)
	if home != "" && !Within(home, parent) {
		parent = filepath.Join(home, "Casper Restores")
	}
	return uniquePath(filepath.Join(parent, name))
}

// FileTarget is where one restored file goes. Replace: its own path. Copy
// into a folder copy (dest != ""): the same relative path under dest. Copy of
// a single file (dest == ""): beside the original, dated by when that
// version was written.
func FileTarget(folderPath, rel, mode, dest string, stamp time.Time) (string, error) {
	rel, err := safeRel(rel)
	if err != nil {
		return "", err
	}
	switch {
	case mode == ModeReplace:
		return filepath.Join(folderPath, rel), nil
	case dest != "":
		return filepath.Join(dest, rel), nil
	default:
		orig := filepath.Join(folderPath, rel)
		return uniquePath(filepath.Join(filepath.Dir(orig), DatedFileName(filepath.Base(orig), stamp))), nil
	}
}

// MoveInto puts src at dst, creating folders. Replace-safe: when dst exists
// it's written beside and renamed over it, so a failure never leaves a
// half-written file.
func MoveInto(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	if err := renameRetry(src, dst); err == nil {
		return nil
	}
	tmp := dst + ".casper-restoring"
	if err := copyKeepTimes(src, tmp); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := renameRetry(tmp, dst); err != nil {
		os.Remove(tmp)
		return err
	}
	os.Remove(src)
	return nil
}

// renameRetry is os.Rename, tried a few times more on Windows, where a file
// that Word, Explorer or a virus scanner has open can't be renamed over for a
// moment (a sharing violation). Elsewhere it's one try.
func renameRetry(src, dst string) error {
	err := os.Rename(src, dst)
	if runtime.GOOS != "windows" {
		return err
	}
	for _, wait := range []time.Duration{50, 150, 400, 1000} {
		if err == nil || !sharingViolation(err) {
			return err
		}
		time.Sleep(wait * time.Millisecond)
		err = os.Rename(src, dst)
	}
	return err
}

func copyKeepTimes(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, info.Mode().Perm()|0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Chtimes(dst, info.ModTime(), info.ModTime())
}

// restoredName matches the dated names Casper gives restored copies. Such a
// copy keeps its old file time, so by time alone it would look as if it had
// been there all along; it was made later, and isn't part of the folder as it was.
var restoredName = regexp.MustCompile(`\(as of \d{4}-\d{2}-\d{2}( \d{2}\.\d{2})?\)( \d+)?(\.[^. ]*)?$`)

// skipLocal: Syncthing's own bookkeeping isn't the person's file, and neither
// are earlier restores.
func skipLocal(rel string, d fs.DirEntry) bool {
	base := filepath.Base(rel)
	if d.IsDir() {
		return base == ".stfolder" || base == ".stversions" || restoredName.MatchString(base)
	}
	if restoredName.MatchString(base) {
		return true
	}
	return strings.HasPrefix(base, ".syncthing.") || isSystemNoise(base) || strings.HasSuffix(base, ".casper-restoring")
}

// localAsOf calls fn for each current file that already existed at asOf and
// isn't in skip (those come from the history instead).
func localAsOf(folderPath string, asOf time.Time, skip map[string]bool, fn func(rel, abs string, info fs.FileInfo) error) error {
	return filepath.WalkDir(folderPath, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		rel, rerr := filepath.Rel(folderPath, p)
		if rerr != nil || rel == "." {
			return nil
		}
		if skipLocal(rel, d) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.IsDir() || !d.Type().IsRegular() || skip[filepath.ToSlash(rel)] {
			return nil
		}
		info, ierr := d.Info()
		if ierr != nil || info.ModTime().After(asOf) {
			return nil
		}
		return fn(rel, p, info)
	})
}

// LocalBytesAsOf is how much CopyLocalAsOf would copy.
func LocalBytesAsOf(folderPath string, asOf time.Time, skip map[string]bool) int64 {
	var n int64
	_ = localAsOf(folderPath, asOf, skip, func(_, _ string, info fs.FileInfo) error { n += info.Size(); return nil })
	return n
}

// CopyLocalAsOf copies the current files that already existed at asOf into
// dest, keeping their times.
func CopyLocalAsOf(folderPath, dest string, asOf time.Time, skip map[string]bool) (files int, bytes int64, err error) {
	err = localAsOf(folderPath, asOf, skip, func(rel, abs string, info fs.FileInfo) error {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dest, rel)), 0o755); err != nil {
			return err
		}
		if err := copyKeepTimes(abs, filepath.Join(dest, rel)); err != nil {
			return err
		}
		files++
		bytes += info.Size()
		return nil
	})
	return
}

func dirOf(p string) string { return filepath.Dir(p) }
