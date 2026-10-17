package setup

import (
	"os"
	"strings"

	"golang.org/x/sys/windows"
)

// externalDrives lists the removable and other non-system drives that are
// ready (a USB stick, an external disk), removable ones first. The system
// drive is skipped: the kit belongs somewhere other than this PC.
func externalDrives() []string {
	mask, err := windows.GetLogicalDrives()
	if err != nil {
		return nil
	}
	system := strings.ToUpper(os.Getenv("SystemDrive"))
	var removable, fixed []string
	for i := 0; i < 26; i++ {
		if mask&(1<<uint(i)) == 0 {
			continue
		}
		letter := string(rune('A' + i))
		root := letter + `:\`
		if strings.ToUpper(letter+":") == system {
			continue
		}
		p, _ := windows.UTF16PtrFromString(root)
		switch windows.GetDriveType(p) {
		case windows.DRIVE_REMOVABLE:
			removable = append(removable, root)
		case windows.DRIVE_FIXED:
			fixed = append(fixed, root)
		default:
			continue // network, optical, RAM disks
		}
		if _, err := os.Stat(root); err != nil { // an empty card reader
			if len(removable) > 0 && removable[len(removable)-1] == root {
				removable = removable[:len(removable)-1]
			} else if len(fixed) > 0 && fixed[len(fixed)-1] == root {
				fixed = fixed[:len(fixed)-1]
			}
		}
	}
	return append(removable, fixed...)
}
