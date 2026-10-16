package commands

import (
	"io/fs"
	"path/filepath"

	"casper-agent/internal/mirror"
)

// walkRegular calls fn for each regular file under root, leaving out the
// Photos library (never mirrored; see mirror.IsPhotosLibrary), and reports
// whether it met one.
func walkRegular(root string, fn func(size int64)) (photos bool, err error) {
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && p != root && mirror.IsPhotosLibrary(d.Name()) {
			photos = true
			return filepath.SkipDir
		}
		if d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				fn(info.Size())
			}
		}
		return nil
	})
	return photos, err
}
