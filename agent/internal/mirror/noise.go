package mirror

import "strings"

// isSystemNoise: files the operating system or an editor makes beside the
// person's own, never part of "the folder as it was".
func isSystemNoise(base string) bool {
	switch strings.ToLower(base) {
	case ".ds_store", "thumbs.db", "desktop.ini":
		return true
	}
	return strings.HasPrefix(base, "~$") // Office's lock file for an open document
}
