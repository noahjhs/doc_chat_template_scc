package dialog

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestScriptAnswers(t *testing.T) {
	dir := t.TempDir()
	sf, lf := filepath.Join(dir, "s.json"), filepath.Join(dir, "log")
	t.Setenv("CASPER_DIALOG_SCRIPT", sf)
	t.Setenv("CASPER_DIALOG_LOG", lf)
	if err := os.WriteFile(sf, []byte(`{"confirm":true,"folder":"C:\\Users\\x\\Documents","list":["a","b"],"password":"pw","dont_ask":"never"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if yes, ok := Confirm("Share?", "Yes", "No", time.Second); !yes || !ok {
		t.Fatalf("Confirm = %v %v", yes, ok)
	}
	if p, ok := ChooseFolder(); !ok || p != `C:\Users\x\Documents` {
		t.Fatalf("ChooseFolder = %q %v", p, ok)
	}
	if rows, ok := ChooseFromList("pick", []string{"a", "b", "c"}); !ok || len(rows) != 2 {
		t.Fatalf("ChooseFromList = %v %v", rows, ok)
	}
	if pw, ok := AskHidden("pass"); !ok || pw != "pw" {
		t.Fatalf("AskHidden = %q %v", pw, ok)
	}
	if acc, never := ConfirmWithDontAskAgain("m", "Go"); acc || !never {
		t.Fatalf("dont_ask = %v %v", acc, never)
	}
	if _, ok := OpenFile("open", dir); ok {
		t.Fatal("a missing answer should cancel")
	}
	Info("skipped")
	b, _ := os.ReadFile(lf)
	for _, want := range []string{"confirm\tShare?", "folder\t", "open\topen", "info\tskipped"} {
		if !strings.Contains(string(b), want) {
			t.Errorf("log missing %q:\n%s", want, b)
		}
	}
}
