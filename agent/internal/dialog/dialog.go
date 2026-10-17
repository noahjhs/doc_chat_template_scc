// Package dialog shows native modals: yes/no with a timeout, folder and file
// panels, a multi-select list, a hidden-field box, errors and the farewell.
// The entry points are the same on every OS; dialog_darwin.go and
// dialog_windows.go supply the lower-case implementations.
//
// For tests, CASPER_DIALOG_SCRIPT=<json file> answers every dialog with no
// window (see script.go), and CASPER_DIALOG_LOG=<file> records what was asked.
package dialog

import "time"

// ShowFarewellDialog says goodbye right before sign-out shuts the process down.
func ShowFarewellDialog(logf func(format string, args ...any)) {
	if scripted("farewell", "") {
		return
	}
	farewell(logf)
}

// ShowFatalError makes a startup failure visible when there is no console.
func ShowFatalError(message string) {
	if scripted("fatal", message) {
		return
	}
	showFatalError(message)
}

// ShowError reports a non-fatal error; the caller keeps running.
func ShowError(message string) {
	if scripted("error", message) {
		return
	}
	showError(message)
}

// Info tells the person something, with one OK button.
func Info(message string) {
	if scripted("info", message) {
		return
	}
	info(message)
}

// ConfirmWithDontAskAgain asks with an action button, a "not now" and a way to
// stop being asked.
func ConfirmWithDontAskAgain(message, actionLabel string) (accepted bool, dontAskAgain bool) {
	if s := script(); s != nil {
		record("dont_ask", message)
		switch s.DontAsk {
		case "accept":
			return true, false
		case "never":
			return false, true
		}
		return false, false
	}
	return confirmWithDontAskAgain(message, actionLabel)
}

// Confirm asks the person a yes/no question and reports their answer;
// answered is false if they didn't respond within the time limit. Used for
// consent that must come from the person, not their agent
// (docs/product/scenarios/mirroring.md, "Setup").
func Confirm(message, yesLabel, noLabel string, within time.Duration) (yes bool, answered bool) {
	if s := script(); s != nil {
		record("confirm", message)
		if s.Confirm == nil {
			return false, true
		}
		return *s.Confirm, true
	}
	return confirm(message, yesLabel, noLabel, within)
}

// ChooseFolder shows the folder chooser, starting in the home folder.
func ChooseFolder() (string, bool) {
	if s := script(); s != nil {
		record("folder", "")
		return s.pathAnswer(s.Folder)
	}
	return chooseFolder()
}

// ChooseFromList shows a list where several rows may be chosen.
func ChooseFromList(prompt string, rows []string) ([]string, bool) {
	if s := script(); s != nil {
		record("list", prompt)
		if len(s.List) == 0 {
			return nil, false
		}
		return s.List, true
	}
	return chooseFromList(prompt, rows)
}

// OpenFile shows the Open panel for one file, starting in defaultDir.
func OpenFile(message, defaultDir string) (string, bool) {
	if s := script(); s != nil {
		record("open", message)
		return s.pathAnswer(s.Open)
	}
	return openFile(message, defaultDir)
}

// SaveFile shows the Save panel, starting in defaultDir with defaultName filled in.
func SaveFile(message, defaultDir, defaultName string) (string, bool) {
	if s := script(); s != nil {
		record("save", message)
		return s.pathAnswer(s.Save)
	}
	return saveFile(message, defaultDir, defaultName)
}

// AskHidden shows a box with a hidden text field and returns what the person
// typed -- the agent driving `Casper setup` never sees it.
func AskHidden(prompt string) (string, bool) {
	if s := script(); s != nil {
		record("password", prompt)
		if s.Password == nil {
			return "", false
		}
		return *s.Password, true
	}
	return askHidden(prompt)
}
