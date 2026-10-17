// casper-dialogtest is a test-only program: it shows one dialog and prints the
// answer as JSON, so a Windows VM run can check the real dialogs (or, with
// CASPER_DIALOG_SCRIPT, the scripted ones). Never shipped in the app.
//
//	casper-dialogtest confirm|dont_ask|folder|open|save|list|password|info|error [text]
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"time"

	"casper-agent/internal/dialog"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: casper-dialogtest <kind> [text]")
		os.Exit(2)
	}
	text := "Casper dialog test"
	if len(os.Args) > 2 {
		text = os.Args[2]
	}
	out := map[string]any{}
	switch os.Args[1] {
	case "confirm":
		out["yes"], out["answered"] = dialog.Confirm(text, "Yes", "No", 2*time.Minute)
	case "dont_ask":
		out["accepted"], out["dont_ask"] = dialog.ConfirmWithDontAskAgain(text, "Do it")
	case "folder":
		out["path"], out["ok"] = dialog.ChooseFolder()
	case "open":
		home, _ := os.UserHomeDir()
		out["path"], out["ok"] = dialog.OpenFile(text, home)
	case "save":
		home, _ := os.UserHomeDir()
		out["path"], out["ok"] = dialog.SaveFile(text, home, "kit.casperkit")
	case "list":
		out["rows"], out["ok"] = dialog.ChooseFromList(text, []string{"one", "two", "three"})
	case "password":
		out["text"], out["ok"] = dialog.AskHidden(text)
	case "info":
		dialog.Info(text)
	case "error":
		dialog.ShowError(text)
	default:
		fmt.Fprintln(os.Stderr, "unknown kind")
		os.Exit(2)
	}
	_ = json.NewEncoder(os.Stdout).Encode(out)
}
