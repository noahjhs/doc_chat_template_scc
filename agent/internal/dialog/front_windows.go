//go:build windows

package dialog

import (
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// A dialog Casper opens because of something the person did in a browser
// (Start, Make Recovery Kit...) opens behind that browser: Windows doesn't
// let a background process take the foreground, so the person sees only a
// flashing taskbar icon. toFront, called before a dialog opens and run again
// when it closes, puts this process's visible windows above everything else
// (always-on-top needs no foreground right) once it has appeared (a few seconds: after that the person may go elsewhere,
// and the dialog stays on top without taking focus again).

var (
	user32                       = windows.NewLazySystemDLL("user32.dll")
	procEnumWindows              = user32.NewProc("EnumWindows")
	procGetWindowThreadProcessId = user32.NewProc("GetWindowThreadProcessId")
	procIsWindowVisible          = user32.NewProc("IsWindowVisible")
	procSetWindowPos             = user32.NewProc("SetWindowPos")
	procSetForegroundWindow      = user32.NewProc("SetForegroundWindow")
)

const (
	swpNoSize     = 0x0001
	swpNoMove     = 0x0002
	swpShowWindow = 0x0040
)

var hwndTopmost = ^uintptr(0) // (HWND)-1

func raiseOwnWindows() {
	pid := windows.GetCurrentProcessId()
	cb := syscall.NewCallback(func(hwnd, _ uintptr) uintptr {
		var owner uint32
		procGetWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&owner)))
		if owner != pid {
			return 1
		}
		if visible, _, _ := procIsWindowVisible.Call(hwnd); visible == 0 {
			return 1
		}
		procSetWindowPos.Call(hwnd, hwndTopmost, 0, 0, 0, 0, swpNoMove|swpNoSize|swpShowWindow)
		procSetForegroundWindow.Call(hwnd) // works when Windows allows it; the z-order above doesn't need it
		return 1
	})
	procEnumWindows.Call(cb, 0)
}

// toFront starts raising this process's windows and returns the function that stops it.
func toFront() func() {
	stop := make(chan struct{})
	var once sync.Once
	go func() {
		t := time.NewTicker(150 * time.Millisecond)
		defer t.Stop()
		deadline := time.After(4 * time.Second)
		for {
			select {
			case <-stop:
				return
			case <-deadline:
				return
			case <-t.C:
				raiseOwnWindows()
			}
		}
	}()
	return func() { once.Do(func() { close(stop) }) }
}
