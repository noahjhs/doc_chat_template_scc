package setup

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/mirror"
)

// A recovery kit must never sit inside a folder Casper mirrors: the mirrored
// copy is encrypted with the very password the kit holds, so after losing the
// Mac it couldn't unlock itself (docs/product/recovery-v2-design.md, P0).

// foldPath resolves a path as far as the filesystem allows (symlinks in the
// longest existing prefix) and lower-cases it, since APFS is case-insensitive
// by default. Over-matching on a case-sensitive volume only errs safe.
func foldPath(p string) string {
	p, err := filepath.Abs(p)
	if err != nil {
		p = filepath.Clean(p)
	}
	rest := ""
	for cur := p; ; {
		if r, err := filepath.EvalSymlinks(cur); err == nil {
			p = filepath.Join(r, rest)
			break
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			break
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
	return strings.ToLower(p)
}

// insideAny reports the first root that path is at or under.
func insideAny(path string, roots []string) (string, bool) {
	fp := foldPath(path)
	for _, r := range roots {
		fr := foldPath(r)
		if fp == fr || strings.HasPrefix(fp, strings.TrimRight(fr, "/")+"/") {
			return r, true
		}
	}
	return "", false
}

// mirroredRoots are the folders on this Mac that Casper mirrors or holds for
// this account: its owned folders' paths, plus where held folders are kept.
func (c *cli) mirroredRoots(username string) ([]string, error) {
	var roots []string
	if dir, err := config.AppConfigDir(); err == nil {
		roots = append(roots, filepath.Join(dir, "mirror", "held"))
	}
	sessions, _ := config.LoadSessions()
	for _, s := range sessions {
		if s.Username != username {
			continue
		}
		var d mirror.Desired
		if err := config.FetchMirrorConfig(c.authDomain, s.DeviceToken, &d); err != nil {
			return nil, fmt.Errorf("couldn't check which folders are mirrored (%w) -- try again when Casper is reachable", err)
		}
		for _, f := range d.Owned {
			if f.Path != "" {
				roots = append(roots, f.Path)
			}
		}
	}
	return roots, nil
}

// kitDefaultDir is where the save panel starts: a connected external drive if
// there is one, else the Desktop.
func kitDefaultDir(volumes string) string {
	if entries, err := os.ReadDir(volumes); err == nil {
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), ".") {
				continue
			}
			p := filepath.Join(volumes, e.Name())
			if r, err := filepath.EvalSymlinks(p); err == nil && r == "/" {
				continue // the startup disk
			}
			if fi, err := os.Stat(p); err == nil && fi.IsDir() {
				return p
			}
		}
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Desktop")
}

func kitFileName(username string) string {
	return fmt.Sprintf("Casper Recovery Kit (%s).txt", username)
}

var errKitPlace = errors.New("that folder is mirrored, so the kit couldn't open itself. Choose somewhere else, like a USB drive.")

// checkKitPath refuses a path inside a mirrored folder.
func checkKitPath(path string, roots []string) error {
	if _, in := insideAny(path, roots); in {
		return errKitPlace
	}
	return nil
}

// chooseKitPath asks where to save the kit (the standard Save panel), and
// asks again while the choice is inside a mirrored folder. A non-empty
// explicit path (--to) skips the panel but is checked the same way.
func (c *cli) chooseKitPath(username, explicit string) (string, error) {
	roots, err := c.mirroredRoots(username)
	if err != nil {
		return "", err
	}
	if explicit != "" {
		return explicit, checkKitPath(explicit, roots)
	}
	for i := 0; i < 5; i++ {
		path, ok := dialog.SaveFile("Where should Casper save your recovery kit? Keep it away from this Mac, like on a USB drive.",
			kitDefaultDir("/Volumes"), kitFileName(username))
		if !ok {
			return "", errors.New("no place was chosen for the kit -- run `setup recovery-kit` again when the person is ready")
		}
		if err := checkKitPath(path, roots); err != nil {
			dialog.ShowError(err.Error())
			continue
		}
		return path, nil
	}
	return "", errKitPlace
}

func (c *cli) kitMarker(username string) string {
	dir, _ := config.AppConfigDir()
	return filepath.Join(dir, "recovery-kit-path-"+username)
}

// savedKitPath is where this Mac last saved the kit (recorded locally,
// never sent), falling back to the old fixed location in Documents.
func (c *cli) savedKitPath(username string) (string, bool) {
	if b, err := os.ReadFile(c.kitMarker(username)); err == nil {
		if p := strings.TrimSpace(string(b)); p != "" {
			if _, err := os.Stat(p); err == nil {
				return p, true
			}
		}
	}
	p := recoveryKitPath(username)
	_, err := os.Stat(p)
	return p, err == nil
}

func (c *cli) recordKitPath(username, path string) {
	_ = os.WriteFile(c.kitMarker(username), []byte(path), 0o600)
}

// offerMoveOldKit: a kit made by an earlier version sits in Documents. If
// Documents is mirrored, offer once to move it somewhere safe. Returns the
// new path if it was moved.
func (c *cli) offerMoveOldKit(username string, roots []string, explicit string) (string, error) {
	old := recoveryKitPath(username)
	if _, err := os.Stat(old); err != nil {
		return "", nil
	}
	if _, in := insideAny(old, roots); !in {
		return "", nil
	}
	dir, _ := config.AppConfigDir()
	offered := filepath.Join(dir, "recovery-kit-move-offered-"+username)
	if _, err := os.Stat(offered); err == nil {
		return "", nil
	}
	_ = os.WriteFile(offered, nil, 0o600)
	if explicit == "" {
		yes, answered := dialog.Confirm("Your recovery kit is in a folder Casper mirrors, so it couldn't open itself after losing this Mac. Move it somewhere safer?", "Move It", "Not Now", 3*time.Minute)
		if !answered || !yes {
			return "", nil
		}
	}
	dest, err := c.chooseKitPath(username, explicit)
	if err != nil {
		return "", err
	}
	if err := moveFile(old, dest); err != nil {
		return "", err
	}
	c.recordKitPath(username, dest)
	return dest, nil
}

func moveFile(from, to string) error {
	if err := os.Rename(from, to); err == nil {
		return nil
	}
	in, err := os.Open(from)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(to)
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Remove(from)
}
