//go:build !windows

package urlscheme

// Ensure has nothing to do here: macOS registers the scheme from the app
// bundle's Info.plist.
func Ensure() error { return nil }
