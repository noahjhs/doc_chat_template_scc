//go:build windows

package mirror

import "math"

// FreeBytes isn't measured on Windows yet: nothing is refused for space.
func FreeBytes(string) int64 { return math.MaxInt64 }
