package commands

import (
	"io/fs"
	"path/filepath"
)

func walkRegular(root string, fn func(size int64)) error {
	return filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				fn(info.Size())
			}
		}
		return nil
	})
}
