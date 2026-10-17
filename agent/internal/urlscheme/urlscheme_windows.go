//go:build windows

// Package urlscheme receives casper:// links. On Windows a link is a command
// line: the protocol handler under HKCU\Software\Classes\<scheme> starts
// `Casper.exe "<link>"`. Register writes that handler (on every start, so it
// follows the app if it moves) and passes along a link this process was itself
// started with; cmd/casper's single-instance code hands links that reach a
// second process to the first.
package urlscheme

import (
	"os"
	"path/filepath"

	"casper-agent/internal/config"

	"golang.org/x/sys/windows/registry"
)

func Register(h func(rawURL string)) error {
	scheme := config.PairURLScheme()
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	for _, u := range FromArgs(os.Args[1:], scheme) {
		h(u)
	}
	return registerScheme(scheme, exe)
}

func setString(path, name, value string) error {
	k, _, err := registry.CreateKey(registry.CURRENT_USER, path, registry.SET_VALUE|registry.QUERY_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	if cur, _, err := k.GetStringValue(name); err == nil && cur == value {
		return nil
	}
	return k.SetStringValue(name, value)
}

// registerScheme makes `scheme://...` open exe with the link as its argument.
func registerScheme(scheme, exe string) error {
	root := `Software\Classes\` + scheme
	exe, _ = filepath.Abs(exe)
	for _, kv := range [][3]string{
		{root, "", "URL:Casper"},
		{root, "URL Protocol", ""},
		{root + `\DefaultIcon`, "", `"` + exe + `",0`},
		{root + `\shell\open\command`, "", `"` + exe + `" "%1"`},
	} {
		if err := setString(kv[0], kv[1], kv[2]); err != nil {
			return err
		}
	}
	return nil
}

func unregisterScheme(scheme string) {
	root := `Software\Classes\` + scheme
	for _, sub := range []string{`\shell\open\command`, `\shell\open`, `\shell`, `\DefaultIcon`, ""} {
		_ = registry.DeleteKey(registry.CURRENT_USER, root+sub)
	}
}

// Ensure writes the protocol handler for this build without waiting for the
// app to have run once: `Casper.exe setup pair` opens a casper:// link that
// must start the app.
func Ensure() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return registerScheme(config.PairURLScheme(), exe)
}

// RegisterFor points the handler for scheme at exe. The installer uses it to
// register the app it just placed, before that app has ever run.
func RegisterFor(scheme, exe string) error { return registerScheme(scheme, exe) }

// Unregister removes scheme's handler (the uninstaller).
func Unregister(scheme string) { unregisterScheme(scheme) }
