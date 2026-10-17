//go:build windows

package urlscheme

import (
	"testing"

	"golang.org/x/sys/windows/registry"
)

func TestRegisterSchemeWritesAndRewritesTheHandler(t *testing.T) {
	const scheme = "casper-unittest"
	defer unregisterScheme(scheme)
	read := func() string {
		k, err := registry.OpenKey(registry.CURRENT_USER, `Software\Classes\`+scheme+`\shell\open\command`, registry.QUERY_VALUE)
		if err != nil {
			t.Fatal(err)
		}
		defer k.Close()
		v, _, _ := k.GetStringValue("")
		return v
	}
	if err := registerScheme(scheme, `C:\Apps\Casper\Casper.exe`); err != nil {
		t.Fatal(err)
	}
	if got, want := read(), `"C:\Apps\Casper\Casper.exe" "%1"`; got != want {
		t.Fatalf("command = %s, want %s", got, want)
	}
	if err := registerScheme(scheme, `D:\Moved\Casper.exe`); err != nil {
		t.Fatal(err)
	}
	if got, want := read(), `"D:\Moved\Casper.exe" "%1"`; got != want {
		t.Fatalf("after a move: command = %s, want %s", got, want)
	}
	k, err := registry.OpenKey(registry.CURRENT_USER, `Software\Classes\`+scheme, registry.QUERY_VALUE)
	if err != nil {
		t.Fatal(err)
	}
	defer k.Close()
	if _, _, err := k.GetStringValue("URL Protocol"); err != nil {
		t.Fatalf("URL Protocol value missing: %v", err)
	}
}
