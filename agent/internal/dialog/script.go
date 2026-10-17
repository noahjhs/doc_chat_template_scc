package dialog

import (
	"encoding/json"
	"fmt"
	"os"
	"sync"
)

// answers is the JSON in CASPER_DIALOG_SCRIPT. A missing or null answer is a
// cancel. Info, error and farewell dialogs are skipped.
type answers struct {
	Confirm  *bool    `json:"confirm"`
	DontAsk  string   `json:"dont_ask"` // "accept", "never"; anything else is "not now"
	Folder   *string  `json:"folder"`
	Open     *string  `json:"open"`
	Save     *string  `json:"save"`
	List     []string `json:"list"`
	Password *string  `json:"password"`
}

func (a *answers) pathAnswer(p *string) (string, bool) {
	if p == nil || *p == "" {
		return "", false
	}
	return *p, true
}

var logMu sync.Mutex

// script reads the answer file afresh each time, so a test can change it
// between dialogs.
func script() *answers {
	path := os.Getenv("CASPER_DIALOG_SCRIPT")
	if path == "" {
		return nil
	}
	a := &answers{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, a)
	}
	return a
}

// scripted handles the dialogs with no answer: when a script is active they
// are logged and skipped.
func scripted(kind, text string) bool {
	if script() == nil {
		return false
	}
	record(kind, text)
	return true
}

func record(kind, text string) {
	path := os.Getenv("CASPER_DIALOG_LOG")
	if path == "" {
		return
	}
	logMu.Lock()
	defer logMu.Unlock()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	fmt.Fprintf(f, "%s\t%s\n", kind, text)
}
