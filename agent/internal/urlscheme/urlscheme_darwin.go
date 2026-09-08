//go:build darwin

// Package urlscheme receives casper:// URLs the OS delivers to this process
// -- the sole pairing/re-pairing mechanism for the daemon (see cmd/casper's
// handlePairURL): a user signs in on the deployed web app, which fires a
// casper://pair?token=...&username=... link, and the OS hands it to this
// already-running (or freshly-launched) process as an Apple Event. Requires
// CFBundleURLTypes registration in Info.plist (see build/build_go_macos.sh)
// and a cgo dependency on Cocoa -- the first in this module.
package urlscheme

/*
#cgo LDFLAGS: -framework Cocoa
#include "shim.h"
*/
import "C"

var handler func(rawURL string)

// Register wires up the Apple Event handler and stores the callback invoked
// for each casper:// URL received. Must be called before systray.Run() --
// see shim.h's doc comment for why.
func Register(h func(rawURL string)) error {
	handler = h
	C.RegisterURLEventHandler()
	return nil
}

//export goHandleOpenURL
func goHandleOpenURL(cURL *C.char) {
	rawURL := C.GoString(cURL)
	if handler != nil {
		handler(rawURL)
	}
}
