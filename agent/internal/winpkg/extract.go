package winpkg

import (
	"archive/zip"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Extract unpacks a zip into dir. Each file is written beside its final name
// and renamed into place (a rename onto a program that was only just stopped
// can fail for a moment on Windows, so it retries), and a name that would
// escape dir is refused.
func Extract(payload []byte, dir string) error {
	zr, err := zip.NewReader(bytes.NewReader(payload), int64(len(payload)))
	if err != nil {
		return fmt.Errorf("the installer's files are damaged: %w", err)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	root, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	for _, f := range zr.File {
		dest := filepath.Join(root, filepath.FromSlash(f.Name))
		if dest != root && !strings.HasPrefix(dest, root+string(filepath.Separator)) {
			return fmt.Errorf("refusing %q: it points outside the install folder", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(dest, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return err
		}
		if err := writeFile(f, dest); err != nil {
			return err
		}
	}
	return nil
}

func writeFile(f *zip.File, dest string) error {
	rc, err := f.Open()
	if err != nil {
		return err
	}
	defer rc.Close()
	tmp := dest + ".new"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, rc); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	var last error
	for i := 0; i < 25; i++ {
		if last = os.Rename(tmp, dest); last == nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	os.Remove(tmp)
	return fmt.Errorf("couldn't replace %s (is Casper still running?): %w", filepath.Base(dest), last)
}

// RemoveDir deletes dir, retrying while Windows lets go of files a stopped
// program held. A missing dir is fine.
func RemoveDir(dir string) error {
	var last error
	for i := 0; i < 25; i++ {
		if last = os.RemoveAll(dir); last == nil || errors.Is(last, os.ErrNotExist) {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return last
}
