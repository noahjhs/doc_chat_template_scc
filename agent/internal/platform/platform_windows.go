//go:build windows

package platform

import (
	"os/exec"
	"syscall"

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
