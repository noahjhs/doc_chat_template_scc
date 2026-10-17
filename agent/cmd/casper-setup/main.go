// Command casper-setup is Casper's Windows installer: one .exe, no admin
// prompt. It carries Casper.exe and syncthing.exe (a zip, staged into payload/
// by build/build_go_windows.sh), puts them in %LOCALAPPDATA%\Programs\Casper,
// registers the casper:// link, the Start menu entry and the Apps & features
// entry, and starts Casper. Switches (Inno Setup's work too, for agents):
//
//	Casper-Setup.exe                    install, with a message at the end
//	Casper-Setup.exe /VERYSILENT        install without any window
//	Casper-Setup.exe /VERYSILENT /NOLAUNCH /DIR="C:\path"
//
// Run again over an existing install it stops the running Casper, replaces the
// files and starts it again. Removal is `Casper.exe uninstall`, also listed in
// Apps & features.
package main

import (
	"embed"
	"fmt"
	"os"

	"casper-agent/internal/config"
	"casper-agent/internal/console"
	"casper-agent/internal/dialog"
	"casper-agent/internal/winpkg"
)

//go:embed all:payload
var payload embed.FS

func main() {
	console.AttachForCLI()
	o := winpkg.ParseOptions(os.Args[1:])
	zip, err := payload.ReadFile("payload/payload.zip")
	if err != nil {
		fail(o, "this installer was built without Casper inside it")
	}
	if err := winpkg.Install(zip, o); err != nil {
		fail(o, err.Error())
	}
	if !o.Silent {
		dialog.Info(config.ProductName() + " is installed. Look for its ghost in the tray, bottom right.")
	}
	fmt.Println("installed")
}

func fail(o winpkg.Options, msg string) {
	fmt.Fprintln(os.Stderr, "install failed:", msg)
	if !o.Silent {
		dialog.ShowError(config.ProductName() + " couldn't be installed: " + msg)
	}
	os.Exit(1)
}
