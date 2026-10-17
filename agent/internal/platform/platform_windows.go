//go:build windows

package platform

import (
	"errors"
	"os/exec"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// url.dll's handler is what ShellExecute uses for a link; no console flash.
func openURL(u string) error {
	cmd := exec.Command("rundll32", "url.dll,FileProtocolHandler", u)
	hideWindow(cmd)
	return cmd.Run()
}

// explorer exits 1 even when it worked, so its status isn't an error.
func reveal(path string) error {
	_ = exec.Command("explorer.exe", "/select,"+path).Run()
	return nil
}

func openFolder(dir string) error {
	_ = exec.Command("explorer.exe", dir).Run()
	return nil
}

func hideWindow(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.HideWindow = true
	cmd.SysProcAttr.CreationFlags |= windows.CREATE_NO_WINDOW
}

var knownFolders = map[string]*windows.KNOWNFOLDERID{
	"Documents": windows.FOLDERID_Documents,
	"Desktop":   windows.FOLDERID_Desktop,
	"Downloads": windows.FOLDERID_Downloads,
	"Pictures":  windows.FOLDERID_Pictures,
	"Music":     windows.FOLDERID_Music,
	"Videos":    windows.FOLDERID_Videos,
	"Movies":    windows.FOLDERID_Videos,
}

func knownFolder(name string) string {
	id, ok := knownFolders[name]
	if !ok {
		return ""
	}
	dir, err := windows.KnownFolderPath(id, windows.KF_FLAG_DEFAULT)
	if err != nil {
		return ""
	}
	return dir
}

func freeBytes(p string) (int64, bool) {
	ptr, err := windows.UTF16PtrFromString(p)
	if err != nil {
		return 0, false
	}
	var avail, total, free uint64
	if windows.GetDiskFreeSpaceEx(ptr, &avail, &total, &free) != nil {
		return 0, false
	}
	return int64(avail), true
}

const fileManagerName = "File Explorer"
const revealLabel = "Show file"
const trayName = "tray at the bottom right (it may be under the ^ arrow)"
const keyStoreName = "Windows credential store"

const listensInboundByDefault = false

var (
	jobOnce   sync.Once
	jobHandle windows.Handle
	jobErr    error
)

// A job object that kills its processes when the last handle to it closes,
// which Windows does when Casper's process ends for any reason.
func job() (windows.Handle, error) {
	jobOnce.Do(func() {
		h, err := windows.CreateJobObject(nil, nil)
		if err != nil {
			jobErr = err
			return
		}
		info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
		info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if _, err := windows.SetInformationJobObject(h, windows.JobObjectExtendedLimitInformation,
			uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
			windows.CloseHandle(h)
			jobErr = err
			return
		}
		jobHandle = h
	})
	return jobHandle, jobErr
}

func killOnExit(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return errors.New("process not started")
	}
	h, err := job()
	if err != nil {
		return err
	}
	ph, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(ph)
	return windows.AssignProcessToJobObject(h, ph)
}

const (
	attrOffline         = 0x1000
	attrRecallOnOpen    = 0x40000
	attrRecallOnAccess  = 0x400000
	cloudOnlyAttributes = attrOffline | attrRecallOnOpen | attrRecallOnAccess
)

func isCloudOnly(path string) bool {
	ptr, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return false
	}
	a, err := windows.GetFileAttributes(ptr)
	return err == nil && a&cloudOnlyAttributes != 0
}
