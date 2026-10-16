//go:build !windows

package mirror

import "syscall"

// FreeBytes is the space a person can still use on the volume holding path
// (or its nearest existing parent).
func FreeBytes(path string) int64 {
	for p := path; ; {
		var st syscall.Statfs_t
		if syscall.Statfs(p, &st) == nil {
			return int64(st.Bavail) * int64(st.Bsize)
		}
		parent := dirOf(p)
		if parent == p {
			return 0
		}
		p = parent
	}
}
