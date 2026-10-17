// Package winpkg installs and removes Casper on Windows: the per-user
// installer (cmd/casper-setup) and `Casper.exe uninstall` share it. Nothing
// here needs administrator rights; everything lives under the person's own
// profile and HKCU. The file handling and argument parsing build everywhere
// so the Mac's test run covers them; the registry, process and shortcut parts
// are Windows only.
package winpkg

import "strings"

// Version is stamped into the installer at build time (-ldflags -X).
var Version = "0.0.0"

// Options is what the command line asked for. The installer accepts Inno
// Setup's switches too (/VERYSILENT, /DIR=...), because that is what an agent
// scripting it will reach for.
type Options struct {
	Silent    bool   // no dialogs
	NoLaunch  bool   // install, but don't start Casper
	Dir       string // install folder; empty means the default under %LOCALAPPDATA%
	PurgeData bool   // uninstall: also remove Casper's own data folder
	From      string // uninstall (internal): the folder being removed, when run from a temporary copy
}

// ParseOptions reads the installer's and uninstaller's arguments. Unknown ones
// are ignored (Inno's /NORESTART and the like).
func ParseOptions(args []string) Options {
	var o Options
	for i := 0; i < len(args); i++ {
		a := args[i]
		low := strings.ToLower(a)
		value := func(prefix string) (string, bool) {
			if strings.HasPrefix(low, prefix+"=") {
				return strings.Trim(a[len(prefix)+1:], `"`), true
			}
			if low == prefix && i+1 < len(args) {
				i++
				return args[i], true
			}
			return "", false
		}
		switch {
		case low == "/verysilent", low == "/silent", low == "/s", low == "--silent", low == "-silent":
			o.Silent = true
		case low == "/nolaunch", low == "--no-launch":
			o.NoLaunch = true
		case low == "--purge-data", low == "/purgedata":
			o.PurgeData = true
		default:
			if v, ok := value("/dir"); ok {
				o.Dir = v
			} else if v, ok := value("--dir"); ok {
				o.Dir = v
			} else if v, ok := value("--from"); ok {
				o.From = v
			}
		}
	}
	return o
}
