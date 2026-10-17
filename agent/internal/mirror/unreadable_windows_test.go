//go:build windows

package mirror

import (
	"os"
	"testing"

	"golang.org/x/sys/windows"
)

// makeUnreadable writes a file and keeps it open with no sharing, as Word
// does with a document: nothing else can read it until the test ends.
func makeUnreadable(t *testing.T, path string) {
	t.Helper()
	if err := os.WriteFile(path, []byte("no"), 0o644); err != nil {
		t.Fatal(err)
	}
	p, _ := windows.UTF16PtrFromString(path)
	h, err := windows.CreateFile(p, windows.GENERIC_READ|windows.GENERIC_WRITE, 0, nil, windows.OPEN_EXISTING, windows.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { windows.CloseHandle(h) })
}
