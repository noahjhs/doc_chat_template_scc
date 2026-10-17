//go:build !windows

package platform

import (
	"os/exec"
	"syscall"
)

func openURL(u string) error { return exec.Command("open", u).Run() }

func reveal(path string) error { return exec.Command("open", "-R", path).Run() }

func openFolder(dir string) error { return exec.Command("open", dir).Run() }

func hideWindow(*exec.Cmd) {}

func knownFolder(string) string { return "" }

func freeBytes(p string) (int64, bool) {
	var st syscall.Statfs_t
	if syscall.Statfs(p, &st) != nil {
		return 0, false
	}
	return int64(st.Bavail) * int64(st.Bsize), true
}

const fileManagerName = "Finder"
const trayName = "menu bar"
const keyStoreName = "Keychain"

func killOnExit(*exec.Cmd) error { return nil }

func isCloudOnly(string) bool { return false }

const listensInboundByDefault = true
