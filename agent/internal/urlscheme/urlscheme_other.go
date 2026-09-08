//go:build !darwin

// No-op stub: casper:// URL-scheme pairing is macOS-only for now (see the
// darwin implementation's doc comment). Keeps cmd/casper buildable on other
// platforms without a build-tag branch at every call site.
package urlscheme

func Register(h func(rawURL string)) error {
	return nil
}
