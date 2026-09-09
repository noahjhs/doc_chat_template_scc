//go:build !darwin

// No-op stub -- see activate_darwin.go's doc comment. Keeps cmd/casper
// buildable on other platforms without a build-tag branch at the call site.
package activate

func DefaultBrowser() {}
