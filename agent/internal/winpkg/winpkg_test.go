package winpkg

import (
	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func makeZip(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		w.Write([]byte(body))
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestParseOptions(t *testing.T) {
	cases := []struct {
		args []string
		want Options
	}{
		{nil, Options{}},
		{[]string{"/VERYSILENT", "/NORESTART", "/NOLAUNCH"}, Options{Silent: true, NoLaunch: true}},
		{[]string{"/SILENT", `/DIR="C:\Apps\Casper"`}, Options{Silent: true, Dir: `C:\Apps\Casper`}},
		{[]string{"--dir", `D:\x`, "--purge-data"}, Options{Dir: `D:\x`, PurgeData: true}},
		{[]string{"--from", `C:\old`, "--silent"}, Options{From: `C:\old`, Silent: true}},
	}
	for _, c := range cases {
		if got := ParseOptions(c.args); !reflect.DeepEqual(got, c.want) {
			t.Errorf("ParseOptions(%v) = %+v, want %+v", c.args, got, c.want)
		}
	}
}

func TestExtractReplacesFilesAndKeepsOthers(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "Casper.exe"), []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := Extract(makeZip(t, map[string]string{"Casper.exe": "new", "sub/syncthing.exe": "st"}), dir); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"Casper.exe": "new", "sub/syncthing.exe": "st"} {
		got, err := os.ReadFile(filepath.Join(dir, filepath.FromSlash(name)))
		if err != nil || string(got) != want {
			t.Errorf("%s = %q, %v; want %q", name, got, err, want)
		}
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "*.new")); len(left) != 0 {
		t.Errorf("temporary files left behind: %v", left)
	}
}

func TestExtractRefusesEscapingNames(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "install")
	if err := Extract(makeZip(t, map[string]string{"../evil.exe": "x"}), dir); err == nil {
		t.Fatal("a name outside the install folder was accepted")
	}
	if _, err := os.Stat(filepath.Join(filepath.Dir(dir), "evil.exe")); err == nil {
		t.Fatal("file written outside the install folder")
	}
}

func TestExtractRejectsGarbage(t *testing.T) {
	if err := Extract([]byte("not a zip"), t.TempDir()); err == nil {
		t.Fatal("garbage accepted")
	}
}

func TestRemoveDirMissingIsFine(t *testing.T) {
	if err := RemoveDir(filepath.Join(t.TempDir(), "nope")); err != nil {
		t.Fatal(err)
	}
}
