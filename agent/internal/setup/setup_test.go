package setup

import (
	"strings"
	"testing"
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
