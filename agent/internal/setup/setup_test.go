package setup

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"casper-agent/internal/platform"
)

func TestPairURLEscapesTokenAndUsername(t *testing.T) {
	u := pairURL("a+b/c=", "riley smith")
	if !strings.HasSuffix(u, "://pair?token=a%2Bb%2Fc%3D&username=riley+smith") {
		t.Fatalf("unexpected pair URL %s", u)
	}
}

func TestAppBundle(t *testing.T) {
	if got := appBundle("/Applications/CasperGo/Casper.app/Contents/MacOS/Casper"); got != "/Applications/CasperGo/Casper.app" {
		t.Fatalf("got %s", got)
	}
	if got := appBundle("/tmp/casper-cli"); got != "/tmp/casper-cli" {
		t.Fatalf("got %s", got)
	}
}

func TestRandomPasswordsAreLongAndDistinct(t *testing.T) {
	a, b := randomPassword(), randomPassword()
	if len(a) < 30 || a == b {
		t.Fatalf("weak or repeated passwords: %q %q", a, b)
	}
}

func TestMainRejectsUnknownCommands(t *testing.T) {
	t.Setenv("CONTROL_TOOL_AUTH_DOMAIN", "localhost:1")
	if code := Main([]string{"frobnicate"}); code != 2 {
		t.Fatalf("expected usage error, got %d", code)
	}
	if code := Main([]string{"help"}); code != 0 {
		t.Fatalf("help should succeed, got %d", code)
	}
}

func TestKitPathRefusedInsideMirroredFolders(t *testing.T) {
	base, _ := filepath.EvalSymlinks(t.TempDir())
	docs := filepath.Join(base, "Documents")
	held := filepath.Join(base, "held")
	outside := filepath.Join(base, "usb")
	for _, d := range []string{docs, filepath.Join(docs, "sub"), held, outside} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	link := filepath.Join(outside, "link")
	if err := os.Symlink(docs, link); err != nil {
		t.Fatal(err)
	}
	roots := []string{docs, held}

	refused := map[string]string{
		"inside":               filepath.Join(docs, "kit.txt"),
		"nested, not yet made": filepath.Join(docs, "sub", "new", "kit.txt"),
		"the folder itself":    docs,
		"via symlink":          filepath.Join(link, "kit.txt"),
		"other case":           filepath.Join(base, "DOCUMENTS", "kit.txt"),
		"dot-dot":              filepath.Join(outside, "..", "Documents", "kit.txt"),
		"in held":              filepath.Join(held, "x", "kit.txt"),
	}
	for name, p := range refused {
		if err := checkKitPath(p, roots); err == nil {
			t.Errorf("%s: %s should be refused", name, p)
		}
	}
	allowed := map[string]string{
		"outside":        filepath.Join(outside, "kit.txt"),
		"sibling prefix": filepath.Join(base, "Documents2", "kit.txt"),
	}
	for name, p := range allowed {
		if err := checkKitPath(p, roots); err != nil {
			t.Errorf("%s: %s should be allowed: %v", name, p, err)
		}
	}
}

func TestKitDefaultDir(t *testing.T) {
	if got, want := kitDefaultDir(nil), platform.KnownFolder("Desktop"); got != want {
		t.Fatalf("no drives: got %s, want %s", got, want)
	}
	if got := kitDefaultDir([]string{"X:\\", "Y:\\"}); got != "X:\\" {
		t.Fatalf("with a drive: got %s", got)
	}
}

func TestExternalDrivesSkipTheStartupDisk(t *testing.T) {
	for _, d := range externalDrives() {
		if d == "/" || strings.EqualFold(strings.TrimRight(d, `\`), os.Getenv("SystemDrive")) {
			t.Fatalf("the startup disk listed as external: %s", d)
		}
	}
}

func TestMoveFile(t *testing.T) {
	d := t.TempDir()
	from, to := filepath.Join(d, "a"), filepath.Join(d, "b")
	_ = os.WriteFile(from, []byte("kit"), 0o600)
	if err := moveFile(from, to); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(to); string(b) != "kit" {
		t.Fatalf("moved content %q", b)
	}
	if _, err := os.Stat(from); err == nil {
		t.Fatal("original should be gone")
	}
}
