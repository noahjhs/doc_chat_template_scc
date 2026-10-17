//go:build windows

package winpkg

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/platform"
	"casper-agent/internal/urlscheme"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

const uninstallRoot = `Software\Microsoft\Windows\CurrentVersion\Uninstall\`

// uninstallName is the Apps & features key for this build ("Casper", "Casper-dev").
func uninstallName() string { return strings.ReplaceAll(config.ProductName(), " ", "-") }

// DefaultDir is %LOCALAPPDATA%\Programs\<product>: per user, so no admin prompt.
func DefaultDir() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, "AppData", "Local")
	}
	return filepath.Join(base, "Programs", config.ProductName())
}

func shortcutPath() string {
	return filepath.Join(os.Getenv("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs", config.ProductName()+".lnk")
}

// Install puts the payload (a zip holding Casper.exe and syncthing.exe) in the
// install folder, replacing and stopping any running copy first, then registers
// the casper:// handler, the Start menu entry and the Apps & features entry,
// and starts Casper. It leaves the login item alone: the app asks about that
// itself the first time it runs (and keeps an existing one pointing here).
func Install(payload []byte, o Options) error {
	dir := o.Dir
	if dir == "" {
		dir = DefaultDir()
	}
	if err := StopRunning(dir); err != nil {
		return err
	}
	if err := Extract(payload, dir); err != nil {
		return err
	}
	exe := filepath.Join(dir, "Casper.exe")
	if _, err := os.Stat(exe); err != nil {
		return fmt.Errorf("the installer's files don't include Casper.exe: %w", err)
	}
	if err := urlscheme.RegisterFor(config.PairURLScheme(), exe); err != nil {
		return err
	}
	if err := makeShortcut(shortcutPath(), exe, dir); err != nil {
		return err
	}
	if err := writeUninstallEntry(dir, exe); err != nil {
		return err
	}
	if !o.NoLaunch {
		return start(exe)
	}
	return nil
}

func writeUninstallEntry(dir, exe string) error {
	k, _, err := registry.CreateKey(registry.CURRENT_USER, uninstallRoot+uninstallName(), registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	var size uint32
	_ = filepath.Walk(dir, func(_ string, fi os.FileInfo, err error) error {
		if err == nil && !fi.IsDir() {
			size += uint32(fi.Size() / 1024)
		}
		return nil
	})
	quiet := `"` + exe + `" uninstall --silent`
	for name, v := range map[string]string{
		"DisplayName":          config.ProductName(),
		"DisplayVersion":       Version,
		"Publisher":            "Casper",
		"InstallLocation":      dir,
		"DisplayIcon":          exe + ",0",
		"UninstallString":      `"` + exe + `" uninstall`,
		"QuietUninstallString": quiet,
	} {
		if err := k.SetStringValue(name, v); err != nil {
			return err
		}
	}
	for name, v := range map[string]uint32{"NoModify": 1, "NoRepair": 1, "EstimatedSize": size} {
		if err := k.SetDWordValue(name, v); err != nil {
			return err
		}
	}
	return nil
}

func makeShortcut(lnk, target, dir string) error {
	if err := os.MkdirAll(filepath.Dir(lnk), 0o755); err != nil {
		return err
	}
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
		`$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CASPER_LNK);`+
			`$s.TargetPath=$env:CASPER_TARGET;$s.WorkingDirectory=$env:CASPER_DIR;$s.Save()`)
	cmd.Env = append(os.Environ(), "CASPER_LNK="+lnk, "CASPER_TARGET="+target, "CASPER_DIR="+dir)
	platform.HideWindow(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("couldn't make the Start menu entry: %v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// start runs Casper on its own, so it outlives the installer.
func start(exe string) error {
	cmd := exec.Command(exe)
	cmd.Dir = filepath.Dir(exe)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.DETACHED_PROCESS | windows.CREATE_NEW_PROCESS_GROUP}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

// StopRunning ends Casper.exe and syncthing.exe when they run from dir (a
// Casper installed elsewhere, or another environment's, is left alone), and
// waits for them to go.
func StopRunning(dir string) error {
	prefix := strings.ToLower(filepath.Clean(dir)) + `\`
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snap)
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	var handles []windows.Handle
	for err = windows.Process32First(snap, &entry); err == nil; err = windows.Process32Next(snap, &entry) {
		name := strings.ToLower(windows.UTF16ToString(entry.ExeFile[:]))
		if name != "casper.exe" && name != "syncthing.exe" {
			continue
		}
		if int(entry.ProcessID) == os.Getpid() {
			continue
		}
		h, herr := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.PROCESS_TERMINATE|windows.SYNCHRONIZE, false, entry.ProcessID)
		if herr != nil {
			continue
		}
		buf := make([]uint16, 1024)
		n := uint32(len(buf))
		if windows.QueryFullProcessImageName(h, 0, &buf[0], &n) != nil ||
			!strings.HasPrefix(strings.ToLower(windows.UTF16ToString(buf[:n])), prefix) {
			windows.CloseHandle(h)
			continue
		}
		_ = windows.TerminateProcess(h, 0)
		handles = append(handles, h)
	}
	for _, h := range handles {
		if ev, _ := windows.WaitForSingleObject(h, 10000); ev == uint32(windows.WAIT_TIMEOUT) {
			windows.CloseHandle(h)
			return errors.New("Casper didn't close; quit it from the tray and try again")
		}
		windows.CloseHandle(h)
	}
	return nil
}

// Uninstall removes Casper from dir: stops it, takes out the login item, the
// casper:// handler, the Start menu entry and the Apps & features entry, then
// deletes the folder. It never touches the person's files or the folders
// Casper mirrors; Casper's own data (its sign-in and keys) goes only with
// PurgeData. It can't run from inside dir; RunUninstall deals with that.
func Uninstall(o Options) error {
	dir := o.Dir
	if dir == "" {
		dir = DefaultDir()
	}
	if err := StopRunning(dir); err != nil {
		return err
	}
	_ = config.RemoveLoginItem()
	urlscheme.Unregister(config.PairURLScheme())
	_ = os.Remove(shortcutPath())
	_ = registry.DeleteKey(registry.CURRENT_USER, uninstallRoot+uninstallName())
	if o.PurgeData {
		if data, err := config.AppConfigDir(); err == nil {
			_ = RemoveDir(data)
		}
	}
	return RemoveDir(dir)
}

// RunUninstall is `Casper.exe uninstall [--silent] [--purge-data]`. A program
// can't delete its own folder while it runs from it, so the installed copy
// asks (unless silent), copies itself to a temporary folder and runs the
// removal from there; that copy then deletes itself.
func RunUninstall(args []string) error {
	o := ParseOptions(args)
	self, err := os.Executable()
	if err != nil {
		return err
	}
	if o.From == "" {
		dir := filepath.Dir(self)
		if !o.Silent {
			yes, answered := dialog.Confirm(config.ProductName()+" will stop and be removed from this PC. Your files and the copies friends hold stay as they are.", "Remove", "Keep", 5*time.Minute)
			if !answered || !yes {
				return nil
			}
			keep, answered := dialog.Confirm("Keep this PC's sign-in and keys, in case you install Casper again? If you forget them, you'd need your recovery kit to bring your copies back.", "Keep them", "Forget", 5*time.Minute)
			o.PurgeData = answered && !keep
		}
		tmp, err := os.MkdirTemp("", "casper-uninstall-")
		if err != nil {
			return err
		}
		copyExe := filepath.Join(tmp, "Casper.exe")
		if err := copyFile(self, copyExe); err != nil {
			return err
		}
		next := []string{"uninstall", "--from", dir}
		if o.Silent {
			next = append(next, "--silent")
		}
		if o.PurgeData {
			next = append(next, "--purge-data")
		}
		cmd := exec.Command(copyExe, next...)
		if o.Silent {
			// a scripted uninstall waits for the removal itself
			platform.HideWindow(cmd)
			return cmd.Run()
		}
		cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.DETACHED_PROCESS}
		return cmd.Start()
	}
	// Running from the temporary copy.
	o.Dir = o.From
	err = Uninstall(o)
	dir := filepath.Dir(self)
	// cmd's own quoting, not Go's: the raw command line.
	cmd := exec.Command("cmd.exe")
	cmd.Dir = os.TempDir()
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true,
		CmdLine: `cmd.exe /c "ping -n 3 127.0.0.1 >nul & rmdir /s /q "` + dir + `""`}
	_ = cmd.Start()
	if err != nil {
		if !o.Silent {
			dialog.ShowError("Casper couldn't be fully removed: " + err.Error())
		}
		return err
	}
	if !o.Silent {
		dialog.Info(config.ProductName() + " is removed.")
	}
	return nil
}

func copyFile(from, to string) error {
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(to, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
