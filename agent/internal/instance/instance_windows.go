//go:build windows

// Package instance keeps one Casper per person running. macOS does this itself
// (LaunchServices hands a second launch, and every casper:// link, to the
// running app); Windows starts a new process for each. The first process
// holds a named mutex and listens on a named pipe; any later one sends its
// message down the pipe and exits.
package instance

import (
	"errors"
	"os"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var waitNamedPipe = windows.NewLazySystemDLL("kernel32.dll").NewProc("WaitNamedPipeW")

var (
	mu    sync.Mutex
	mutex windows.Handle
)

func pipeName(name string) string {
	user := strings.Map(func(r rune) rune {
		if r == '\\' || r == '/' {
			return '_'
		}
		return r
	}, os.Getenv("USERNAME"))
	return `\\.\pipe\` + name + "-" + user
}

// Acquire claims the single instance called name. It returns true for the first
// process, which from then on calls onMessage (on its own goroutine) with each
// line a later process sends; false means another process holds it.
func Acquire(name string, onMessage func(string)) (bool, error) {
	ptr, err := windows.UTF16PtrFromString(`Local\` + name)
	if err != nil {
		return false, err
	}
	h, err := windows.CreateMutex(nil, false, ptr)
	if h == 0 {
		return false, err
	}
	if errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		windows.CloseHandle(h)
		return false, nil
	}
	mu.Lock()
	mutex = h
	mu.Unlock()
	go serve(pipeName(name), onMessage)
	return true, nil
}

// Release gives the claim up (so a restart's new process becomes the first).
func Release() {
	mu.Lock()
	defer mu.Unlock()
	if mutex != 0 {
		windows.CloseHandle(mutex)
		mutex = 0
	}
}

func serve(pipe string, onMessage func(string)) {
	ptr, _ := windows.UTF16PtrFromString(pipe)
	for {
		h, err := windows.CreateNamedPipe(ptr, windows.PIPE_ACCESS_INBOUND,
			windows.PIPE_TYPE_BYTE|windows.PIPE_READMODE_BYTE|windows.PIPE_WAIT|windows.PIPE_REJECT_REMOTE_CLIENTS,
			windows.PIPE_UNLIMITED_INSTANCES, 4096, 4096, 0, nil)
		if err != nil {
			time.Sleep(time.Second)
			continue
		}
		err = windows.ConnectNamedPipe(h, nil)
		if err == nil || errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			var msg []byte
			buf := make([]byte, 4096)
			for len(msg) < 64*1024 {
				var n uint32
				if windows.ReadFile(h, buf, &n, nil) != nil || n == 0 {
					break
				}
				msg = append(msg, buf[:n]...)
			}
			if text := strings.TrimSpace(string(msg)); text != "" {
				go onMessage(text)
			}
		}
		windows.DisconnectNamedPipe(h)
		windows.CloseHandle(h)
	}
}

// Send hands message to the process that holds name. It waits a few seconds
// for the pipe, since the holder may have only just started.
func Send(name, message string) error {
	ptr, _ := windows.UTF16PtrFromString(pipeName(name))
	var last error
	for i := 0; i < 50; i++ {
		h, err := windows.CreateFile(ptr, windows.GENERIC_WRITE, 0, nil, windows.OPEN_EXISTING, windows.FILE_ATTRIBUTE_NORMAL, 0)
		if err == nil {
			defer windows.CloseHandle(h)
			var n uint32
			return windows.WriteFile(h, []byte(message+"\n"), &n, nil)
		}
		last = err
		if errors.Is(err, windows.ERROR_PIPE_BUSY) {
			waitNamedPipe.Call(uintptr(unsafe.Pointer(ptr)), 200)
		} else {
			time.Sleep(100 * time.Millisecond)
		}
	}
	return last
}
