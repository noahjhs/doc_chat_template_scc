//go:build windows

package dialog

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"time"

	"github.com/ncruces/zenity"
)

// Native Win32 dialogs through zenity (pure Go, no cgo, no PowerShell): the
// task dialog, the folder and file choosers (IFileDialog), a list box and a
// password box. docs/product/windows.md, row 6.

var title = zenity.Title("Casper")

func farewell(logf func(format string, args ...any)) {
	if err := zenity.Info("See you next time!", title); err != nil && logf != nil {
		logf("show_farewell_dialog: couldn't show it: %s", err)
	}
}

func showFatalError(message string) { _ = zenity.Error(message, title) }

func showError(message string) { _ = zenity.Warning(message, title) }

func info(message string) { _ = zenity.Info(message, title) }

// confirmWithDontAskAgain uses a third button for "Don't ask again": the
// task dialog's checkbox isn't reachable through zenity.
func confirmWithDontAskAgain(message, actionLabel string) (accepted bool, dontAskAgain bool) {
	err := zenity.Question(message, title, zenity.QuestionIcon,
		zenity.OKLabel(actionLabel), zenity.CancelLabel("Not Now"), zenity.ExtraButton("Don't ask again"))
	switch {
	case err == nil:
		return true, false
	case errors.Is(err, zenity.ErrExtraButton):
		return false, true
	default:
		return false, false
	}
}

func confirm(message, yesLabel, noLabel string, within time.Duration) (yes bool, answered bool) {
	ctx, cancel := context.WithTimeout(context.Background(), within)
	defer cancel()
	err := zenity.Question(message, title, zenity.QuestionIcon,
		zenity.OKLabel(yesLabel), zenity.CancelLabel(noLabel), zenity.Context(ctx))
	switch {
	case err == nil:
		return true, true
	case errors.Is(err, context.DeadlineExceeded) || ctx.Err() != nil:
		return false, false
	default:
		return false, true // Cancel, or the window's close box: an explicit "no"
	}
}

func chooseFolder() (string, bool) {
	opts := []zenity.Option{zenity.Directory(), zenity.Title("Choose a folder for Casper to keep safe")}
	if home, _ := os.UserHomeDir(); home != "" {
		opts = append(opts, zenity.Filename(home))
	}
	path, err := zenity.SelectFile(opts...)
	if err != nil || path == "" {
		return "", false
	}
	return filepath.Clean(path), true
}

func chooseFromList(prompt string, rows []string) ([]string, bool) {
	picked, err := zenity.ListMultiple(prompt, rows, title, zenity.OKLabel("Restore"), zenity.CancelLabel("Cancel"), zenity.DisallowEmpty())
	if err != nil || len(picked) == 0 {
		return nil, false
	}
	return picked, true
}

func openFile(message, defaultDir string) (string, bool) {
	path, err := zenity.SelectFile(zenity.Title(message), zenity.Filename(defaultDir+`\`))
	if err != nil || path == "" {
		return "", false
	}
	return path, true
}

func saveFile(message, defaultDir, defaultName string) (string, bool) {
	path, err := zenity.SelectFileSave(zenity.Title(message), zenity.Filename(filepath.Join(defaultDir, defaultName)), zenity.ConfirmOverwrite())
	if err != nil || path == "" {
		return "", false
	}
	return path, true
}

// The password box's label is a single line about 30 characters wide and cuts
// anything longer with "...", and some of Casper's prompts are sentences. A
// longer prompt is shown first in a window that wraps, then the box asks with
// a short label.
const maxLabel = 30

func askHidden(prompt string) (string, bool) {
	label := prompt
	if len(prompt) > maxLabel {
		if err := zenity.Question(prompt, title, zenity.NoIcon, zenity.OKLabel("Continue"), zenity.CancelLabel("Cancel")); err != nil {
			return "", false
		}
		label = "Type it here:"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	text, err := zenity.Entry(label, title, zenity.HideText(), zenity.Context(ctx))
	if err != nil {
		return "", false
	}
	return text, true
}
