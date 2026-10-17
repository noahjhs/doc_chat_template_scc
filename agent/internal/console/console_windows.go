//go:build windows

// Package console gives the Windows build, which has no console of its own
// (-H=windowsgui keeps a tray app from flashing a window), a way to print when
// it is run as a command: `Casper.exe setup status` in a terminal.
package console

import (
	"os"

	"golang.org/x/sys/windows"
)

func usable(h windows.Handle) bool {
	if h == 0 || h == windows.InvalidHandle {
		return false
	}
	_, err := windows.GetFileType(h)
	return err == nil
}

// AttachForCLI connects stdout, stderr and stdin to the parent's console when
// the process was started without any of them (an agent's pipes are kept).
func AttachForCLI() {
	out, _ := windows.GetStdHandle(windows.STD_OUTPUT_HANDLE)
	errh, _ := windows.GetStdHandle(windows.STD_ERROR_HANDLE)
	in, _ := windows.GetStdHandle(windows.STD_INPUT_HANDLE)
	if usable(out) && usable(errh) && usable(in) {
		return
	}
	const attachParent = ^uint32(0)
	if r, _, _ := windows.NewLazySystemDLL("kernel32.dll").NewProc("AttachConsole").Call(uintptr(attachParent)); r == 0 {
		return // no parent console (started by a service or the shell): nothing to print to
	}
	open := func(name string, flag int) *os.File {
		f, err := os.OpenFile(name, flag, 0)
		if err != nil {
			return nil
		}
		return f
	}
	if !usable(out) {
		if f := open("CONOUT$", os.O_WRONLY); f != nil {
			os.Stdout = f
		}
	}
	if !usable(errh) {
		if f := open("CONOUT$", os.O_WRONLY); f != nil {
			os.Stderr = f
		}
	}
	if !usable(in) {
		if f := open("CONIN$", os.O_RDONLY); f != nil {
			os.Stdin = f
		}
	}
}
