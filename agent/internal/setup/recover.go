package setup

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"casper-agent/internal/backup"
	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/mirrord"
)

// Recovery on a replacement Mac, with no agent (docs/product/recovery-v2-design.md,
// P1): the person opens their kit in Casper's own window, then says Restore
// there. Casper's service starts the rebuild and, once it has settled,
// makes this Mac the folders' Mac (mirroring.promote_restored).

// Kit is what a recovery kit holds.
type Kit = recoveryKit

// NewKit is a version-2 kit for one account: its folder passwords (folder ID
// -> password) and backup keys.
func NewKit(authDomain, username string, backupKeys []byte, passwords map[string]string) Kit {
	return recoveryKit{Version: 2, Account: username, AuthDomain: authDomain, BackupKeys: backupKeys, MirrorPasswords: passwords}
}

// kitFileText is the kit file: a short explanation, then the locked kit.
func kitFileText(kit Kit, host string, made time.Time, passphrase string) (string, error) {
	data, _ := json.Marshal(kit)
	armored, err := backup.ExportSecret(data, passphrase)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf(`Casper recovery kit -- %s (made on %s, %s)

This file holds what's needed to get your data back if this Mac is lost:
the passwords of the %d folder(s) you mirror to friends, and your backup keys,
all locked with the passphrase you chose. With this file and that passphrase,
a new Mac can rebuild your mirrored folders from your friends' computers:
choose "Recover from a Lost Mac" in Casper's menu on the new Mac. Keep a copy
of this file, and the passphrase, somewhere other than this Mac (a password
manager is ideal).
Make a new kit whenever you start mirroring another folder.

%s`, kit.Account, host, made.Format("2 Jan 2006"), len(kit.MirrorPasswords), armored), nil
}

// WriteKitFile locks a kit with a passphrase and saves it (headless tests).
func WriteKitFile(path string, kit Kit, passphrase string) error {
	text, err := kitFileText(kit, hostname(), time.Now(), passphrase)
	if err != nil {
		return err
	}
	return os.WriteFile(path, []byte(text), 0o600)
}

// ReadKitFile opens a kit file. A version-1 kit holds only backup keys.
func ReadKitFile(path, passphrase string) (Kit, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Kit{}, err
	}
	i := strings.Index(string(raw), "-----BEGIN AGE ENCRYPTED FILE-----")
	if i < 0 {
		return Kit{}, errors.New("that file isn't a Casper recovery kit")
	}
	data, err := backup.ImportSecret(string(raw[i:]), passphrase)
	if err != nil {
		return Kit{}, errors.New("couldn't open the kit -- wrong passphrase?")
	}
	var kit Kit
	if json.Unmarshal(data, &kit) != nil || kit.Version != 2 {
		kit = Kit{BackupKeys: data}
	}
	return kit, nil
}

// restorePath is where a folder comes back: its original place if that is
// empty or missing here (and inside home), else ~/Casper Restores/<label>.
func restorePath(f config.RestorableFolder, home string) string {
	if f.Path != "" && filepath.IsAbs(f.Path) && (f.Path == home || strings.HasPrefix(f.Path, strings.TrimRight(home, "/")+"/")) && f.Path != home {
		entries, err := os.ReadDir(f.Path)
		if os.IsNotExist(err) || (err == nil && len(entries) == 0) {
			return f.Path
		}
	}
	return filepath.Join(home, "Casper Restores", f.Label)
}

// PlanRestore decides, for the folders this kit can open, where each comes
// back. Folders the kit has no password for are returned separately: their
// mirrors can't be read here.
func PlanRestore(folders []config.RestorableFolder, kit Kit, home string) (where map[string]string, labels []string, uncovered []config.RestorableFolder) {
	where = map[string]string{}
	for _, f := range folders {
		if _, ok := kit.MirrorPasswords[f.ID]; !ok {
			uncovered = append(uncovered, f)
			continue
		}
		where[f.ID] = restorePath(f, home)
		labels = append(labels, f.Label)
	}
	return
}

// RecoverArgs is one recovery on this Mac.
type RecoverArgs struct {
	AuthDomain, Username, DeviceToken, Home string
	Kit                                     Kit
	Passwords                               mirrord.Passwords
	// Confirm is the Restore intent, answered only in Casper's own window
	// (a dialog); it is shown the plan in plain words.
	Confirm func(summary string) bool
}

// Recover puts the kit's folder passwords on this Mac, asks Casper which
// folders could come back and, on the person's yes, starts rebuilding them.
func Recover(a RecoverArgs) (started map[string]string, err error) {
	if a.Kit.Account != "" && a.Kit.Account != a.Username {
		return nil, fmt.Errorf("this kit is for %s, but this Mac is signed in as %s", a.Kit.Account, a.Username)
	}
	for folderID, pw := range a.Kit.MirrorPasswords {
		if err := a.Passwords.Set(mirrord.Account(a.AuthDomain, a.Username, folderID), pw); err != nil {
			return nil, err
		}
	}
	folders, err := config.ListRestorable(a.AuthDomain, a.DeviceToken, true) // also tells Casper the key is here
	if err != nil {
		return nil, err
	}
	where, labels, uncovered := PlanRestore(folders, a.Kit, a.Home)
	if len(where) == 0 {
		if len(uncovered) > 0 {
			return nil, errors.New("this kit doesn't cover your folders (it was made before they were mirrored)")
		}
		return nil, errors.New("there's nothing to recover on this account")
	}
	var lines []string
	for _, f := range folders {
		if p, ok := where[f.ID]; ok {
			lines = append(lines, fmt.Sprintf("%s  →  %s", f.Label, p))
		}
	}
	summary := "Bring back " + strings.Join(labels, ", ") + " on this Mac?\n\n" + strings.Join(lines, "\n") +
		"\n\nThey come back from your friends' computers, and this Mac keeps them mirrored afterwards."
	if len(uncovered) > 0 {
		summary += "\n\n(The kit doesn't cover " + fmt.Sprint(len(uncovered)) + " other folder(s).)"
	}
	if a.Confirm == nil || !a.Confirm(summary) {
		return nil, errors.New("cancelled -- nothing was restored")
	}
	if err := config.StartRestore(a.AuthDomain, a.DeviceToken, where); err != nil {
		return nil, err
	}
	return where, nil
}

// recover is `Casper setup recover [--kit FILE]`: Casper's "Recover from a
// Lost Mac" flow. The kit file, its passphrase and the Restore yes are all
// given in Casper's own dialogs.
func (c *cli) recover(kitPath string) error {
	sessions, _ := config.LoadSessions()
	var s *config.Session
	for i := range sessions {
		if c.account == "" || sessions[i].Username == c.account {
			if s != nil {
				return errors.New("this Mac is signed in to more than one Casper account -- say which with --account")
			}
			s = &sessions[i]
		}
	}
	if s == nil {
		return errors.New("this Mac isn't signed in to Casper yet -- sign in first")
	}
	if kitPath == "" {
		p, ok := dialog.OpenFile("Choose your Casper recovery kit", kitDefaultDir("/Volumes"))
		if !ok {
			return errors.New("cancelled -- no kit was chosen")
		}
		kitPath = p
	}
	pass, err := askHidden("Passphrase for your Casper recovery kit:")
	if err != nil {
		return err
	}
	kit, err := ReadKitFile(kitPath, pass)
	if err != nil {
		return err
	}
	home, _ := os.UserHomeDir()
	where, err := Recover(RecoverArgs{
		AuthDomain: c.authDomain, Username: s.Username, DeviceToken: s.DeviceToken, Home: home, Kit: kit,
		Passwords: mirrord.KeychainPasswords{},
		Confirm: func(summary string) bool {
			yes, answered := dialog.Confirm(summary, "Restore", "Not Now", 10*time.Minute)
			return yes && answered
		},
	})
	if err != nil {
		return err
	}
	c.emit(fmt.Sprintf("Restoring %d folder(s) onto this Mac from your friends' computers. Casper keeps them mirrored once they're back.", len(where)),
		map[string]any{"ok": true, "folders": where})
	return nil
}
