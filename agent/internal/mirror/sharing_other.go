//go:build !windows

package mirror

func sharingViolation(error) bool { return false }
