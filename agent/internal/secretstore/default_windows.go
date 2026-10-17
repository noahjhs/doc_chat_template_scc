//go:build windows

package secretstore

import (
	"os"
	"path/filepath"
	"unsafe"

	"golang.org/x/sys/windows"
)

type dpapi struct{}

func blob(b []byte) *windows.DataBlob {
	if len(b) == 0 {
		return &windows.DataBlob{}
	}
	return &windows.DataBlob{Size: uint32(len(b)), Data: &b[0]}
}

// DPAPI refuses an empty input, so a version byte always rides in front.
func (dpapi) Seal(plain []byte) ([]byte, error) {
	var out windows.DataBlob
	if err := windows.CryptProtectData(blob(append([]byte{1}, plain...)), nil, nil, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out); err != nil {
		return nil, err
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	return append([]byte(nil), unsafe.Slice(out.Data, out.Size)...), nil
}

func (dpapi) Unseal(sealed []byte) ([]byte, error) {
	var out windows.DataBlob
	if err := windows.CryptUnprotectData(blob(sealed), nil, nil, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &out); err != nil {
		return nil, err
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	plain := unsafe.Slice(out.Data, out.Size)
	if len(plain) == 0 || plain[0] != 1 {
		return nil, windows.ERROR_INVALID_DATA
	}
	return append([]byte(nil), plain[1:]...), nil
}

// Default is the store under %LOCALAPPDATA%\Casper, sealed with DPAPI.
func Default() (Store, error) {
	base, err := os.UserCacheDir() // %LOCALAPPDATA%
	if err != nil {
		return Store{}, err
	}
	return Store{Root: filepath.Join(base, "Casper"), Sealer: dpapi{}}, nil
}
