package mirror

import "casper-agent/internal/platform"

// FreeBytes is the space a person can still use on the volume holding path
// (or its nearest existing parent).
func FreeBytes(path string) int64 { return platform.FreeBytes(path) }
