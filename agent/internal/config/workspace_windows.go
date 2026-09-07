//go:build windows

package config

import (
	"fmt"
	"os/exec"
	"strings"
)

// chooseWorkspaceFolder prompts via a native Windows folder-browser dialog
// (System.Windows.Forms.FolderBrowserDialog, run through PowerShell -- no
// cgo/native Windows API bindings needed for one dialog box).
func chooseWorkspaceFolder() (string, error) {
	script := "Add-Type -AssemblyName System.Windows.Forms | Out-Null; " +
		"$d = New-Object System.Windows.Forms.FolderBrowserDialog; " +
		"$d.Description = 'Choose a folder for Casper to work in:'; " +
		"if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) " +
		"{ Write-Output $d.SelectedPath } else { exit 1 }"
	out, err := exec.Command("powershell", "-NoProfile", "-Command", script).Output()
	if err != nil {
		return "", fmt.Errorf("no workspace folder chosen")
	}
	path := strings.TrimSpace(string(out))
	if path == "" {
		return "", fmt.Errorf("no workspace folder chosen")
	}
	return path, nil
}
