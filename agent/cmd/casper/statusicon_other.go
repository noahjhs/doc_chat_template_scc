//go:build !darwin

package main

// setStatusDotIcon has no non-darwin implementation -- see
// statusicon_darwin.go's doc comment for why this needs direct AppKit
// access at all. A silent no-op elsewhere, same posture as the rest of
// this codebase's darwin-only native-integration code (there's no packaged
// Windows build yet -- see build/build_go_macos.sh).
func setStatusDotIcon(titlePrefix string, png []byte) {}
