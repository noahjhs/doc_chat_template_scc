// Package mirrord is the daemon's mirroring glue: it keeps this Mac's
// Syncthing (internal/mirror) reconciled to the desired state casper_service
// computes for each paired identity, reports protection status back, and
// implements the identity-scoped mirroring actions (commands.MirrorOps).
// Shared by the real app (cmd/casper) and the headless test daemon.
package mirrord

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/mirror"
)

type Identity struct{ Username, DeviceToken string }

type Service struct {
	AuthDomain string
	Mgr        *mirror.Manager
	Bin        string
	Passwords  Passwords
	Identities func() []Identity
	// Ask shows the person a native yes/no dialog (dialog.Confirm in the app;
	// a no-op in the headless test daemon, whose test answers consent itself).
	Ask      func(text string) (yes, answered bool)
	Logf     func(string, ...any)
	Interval time.Duration

	once    sync.Once
	refresh chan struct{}
	mu      sync.Mutex
	desired map[string]mirror.Desired // per username, with passwords filled in
	pruned  time.Time
}

func (s *Service) init() {
	s.once.Do(func() {
		s.refresh = make(chan struct{}, 1)
		if s.Interval == 0 {
			s.Interval = 15 * time.Second
		}
		if s.Logf == nil {
			s.Logf = func(string, ...any) {}
		}
	})
}

// Run starts Syncthing and reconciles forever (call in a goroutine).
func (s *Service) Run() {
	s.init()
	for {
		if err := s.Mgr.Start(); err != nil {
			s.Logf("mirroring: couldn't start syncthing: %s (retrying in a minute)", err)
			time.Sleep(time.Minute)
			continue
		}
		break
	}
	s.Logf("mirroring: syncthing up as %s", s.Mgr.DeviceID()[:7])
	t := time.NewTicker(s.Interval)
	defer t.Stop()
	for {
		s.Step()
		select {
		case <-t.C:
		case <-s.refresh:
		}
	}
}

func (s *Service) Refresh() {
	s.init()
	select {
	case s.refresh <- struct{}{}:
	default:
	}
}

func (s *Service) tokenFor(username string) string {
	for _, id := range s.Identities() {
		if id.Username == username {
			return id.DeviceToken
		}
	}
	return ""
}

// Step is one reconcile pass across every paired identity.
func (s *Service) Step() {
	s.init()
	merged := mirror.Desired{}
	peerIdx := map[string]int{}
	per := map[string]mirror.Desired{}
	for _, id := range s.Identities() {
		if err := config.ReportMirrorDevice(s.AuthDomain, id.DeviceToken, s.Mgr.DeviceID(), s.Mgr.Addresses()); err != nil {
			s.Logf("mirroring: reporting device for %s: %s", id.Username, err)
		}
		var d mirror.Desired
		if err := config.FetchMirrorConfig(s.AuthDomain, id.DeviceToken, &d); err != nil {
			s.Logf("mirroring: fetching config for %s: %s", id.Username, err)
			continue
		}
		owned := d.Owned[:0]
		for _, f := range d.Owned {
			pw, err := GetOrCreate(s.Passwords, Account(s.AuthDomain, id.Username, f.ID), !f.Restore)
			if err != nil || pw == "" {
				s.Logf("mirroring: no password for %s (%v) -- %s", f.ID, err, map[bool]string{true: "restore it from the recovery kit", false: "skipping"}[f.Restore])
				continue
			}
			f.Password = pw
			owned = append(owned, f)
		}
		d.Owned = owned
		per[id.Username] = d
		for _, p := range d.Peers {
			if i, ok := peerIdx[p.DeviceID]; ok {
				merged.Peers[i].Addresses = union(merged.Peers[i].Addresses, p.Addresses)
				continue
			}
			peerIdx[p.DeviceID] = len(merged.Peers)
			merged.Peers = append(merged.Peers, p)
		}
		merged.Owned = append(merged.Owned, d.Owned...)
		merged.Held = append(merged.Held, d.Held...)
		merged.Relays = union(merged.Relays, d.Relays)
	}
	if err := s.Mgr.Apply(merged); err != nil {
		s.Logf("mirroring: applying config: %s", err)
	}
	s.mu.Lock()
	s.desired = per
	s.mu.Unlock()

	for username, d := range per {
		var statuses []map[string]any
		for _, f := range d.Owned {
			if f.Restore {
				need, state, err := s.Mgr.FolderState(f.ID)
				overlaid := 0
				if f.Catcher != "" && err == nil && need == 0 && state == "idle" {
					overlaid, _ = s.Mgr.OverlayCatcher(f)
				}
				statuses = append(statuses, map[string]any{"folder_id": f.ID, "restoring": true, "need_files": need, "state": state,
					"local_files": len(filesUnder(f.Path)), "overlaid_from_catcher": overlaid, "error": errString(err)})
				continue
			}
			st, risk := s.Mgr.OwnedStatus(f)
			if f.Catcher != "" {
				if err := s.Mgr.MaintainStaging(f, risk); err != nil {
					s.Logf("mirroring: staging for %s: %s", f.ID, err)
				}
			}
			b, _ := json.Marshal(st)
			var m map[string]any
			_ = json.Unmarshal(b, &m)
			statuses = append(statuses, m)
		}
		if err := config.ReportMirrorStatus(s.AuthDomain, s.tokenFor(username), statuses); err != nil {
			s.Logf("mirroring: reporting status for %s: %s", username, err)
		}
	}
	wanted := map[string]bool{}
	for _, h := range merged.Held {
		if h.Kind == "catcher" {
			wanted[mirror.CatchFolderID(h.ID)] = true
		} else {
			wanted[h.ID] = true
		}
	}
	for _, id := range s.Mgr.CleanupOrphans(wanted, 7*24*time.Hour, time.Now()) {
		s.Logf("mirroring: deleted %s (no longer held for anyone, past the 7-day grace)", id)
	}
	if time.Since(s.pruned) > time.Hour {
		s.pruned = time.Now()
		for _, h := range merged.Held {
			if h.Kind == "mirror" {
				if n := mirror.PruneVersions(s.Mgr.HeldPath(h.ID), time.Now()); n > 0 {
					s.Logf("mirroring: pruned %d old versions of %s", n, h.ID)
				}
			}
		}
	}
}

func union(a, b []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, x := range append(a, b...) {
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	return out
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func filesUnder(root string) []string {
	var out []string
	_ = filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err == nil && info.Mode().IsRegular() && !strings.Contains(p, "/.stfolder") {
			out = append(out, p)
		}
		return nil
	})
	return out
}

// --- Identity-scoped actions (commands.MirrorOps) -----------------------------

// Ops returns the mirroring actions for one paired identity.
func (s *Service) Ops(username string) commands.MirrorOps { return &ops{s: s, user: username} }

type ops struct {
	s    *Service
	user string
}

var errNotYours = errors.New("no such mirrored folder for this account on this Mac")

func (o *ops) desired() mirror.Desired {
	o.s.mu.Lock()
	defer o.s.mu.Unlock()
	return o.s.desired[o.user]
}

func (o *ops) held(folderID string) (mirror.HeldFolder, error) {
	for _, h := range o.desired().Held {
		if h.ID == folderID && h.Kind == "mirror" {
			return h, nil
		}
	}
	return mirror.HeldFolder{}, errNotYours
}

func (o *ops) owned(folderID string) (mirror.OwnedFolder, error) {
	for _, f := range o.desired().Owned {
		if f.ID == folderID {
			return f, nil
		}
	}
	return mirror.OwnedFolder{}, errNotYours
}

func (o *ops) Refresh() { o.s.Refresh() }

func (o *ops) AskConsent(approvalID, text string) {
	if o.s.Ask == nil {
		return
	}
	go func() {
		yes, answered := o.s.Ask(text)
		if !answered {
			return // they can still answer in Telegram
		}
		if err := config.PostConsent(o.s.AuthDomain, o.s.tokenFor(o.user), approvalID, yes); err != nil {
			o.s.Logf("mirroring: recording consent: %s", err)
		}
	}()
}

func (o *ops) VersionTrailers(folderID string) ([]map[string]any, error) {
	h, err := o.held(folderID)
	if err != nil {
		return nil, err
	}
	root := o.s.Mgr.HeldPath(h.ID)
	out := []map[string]any{}
	for _, v := range mirror.ListVersions(root) {
		tr, err := mirror.ReadTrailer(mirror.VersionFile(root, v.EncryptedPath, v.At))
		if err != nil {
			continue
		}
		out = append(out, map[string]any{"encrypted_path": v.EncryptedPath, "at": v.At.Format(time.RFC3339), "size": v.Size,
			"trailer": base64.StdEncoding.EncodeToString(tr)})
	}
	return out, nil
}

const chunk = 4 << 20

func (o *ops) ReadVersion(folderID, encPath string, at time.Time, offset int64) ([]byte, int64, error) {
	h, err := o.held(folderID)
	if err != nil {
		return nil, 0, err
	}
	p := mirror.VersionFile(o.s.Mgr.HeldPath(h.ID), encPath, at)
	if p == "" {
		return nil, 0, errNotYours
	}
	f, err := os.Open(p)
	if err != nil {
		return nil, 0, fmt.Errorf("that version isn't stored here (any more)")
	}
	defer f.Close()
	info, _ := f.Stat()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return nil, 0, err
	}
	buf := make([]byte, chunk)
	n, err := io.ReadFull(f, buf)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return nil, 0, err
	}
	return buf[:n], info.Size(), nil
}

func (o *ops) DecryptTrailers(folderID string, items json.RawMessage) ([]map[string]any, error) {
	f, err := o.owned(folderID)
	if err != nil {
		return nil, err
	}
	var in []struct {
		EncryptedPath string `json:"encrypted_path"`
		At            string `json:"at"`
		Size          int64  `json:"size"`
		Trailer       string `json:"trailer"`
	}
	if err := json.Unmarshal(items, &in); err != nil {
		return nil, err
	}
	out := []map[string]any{}
	for _, it := range in {
		tr, err := base64.StdEncoding.DecodeString(it.Trailer)
		if err != nil {
			continue
		}
		info, err := mirror.DecryptTrailer(tr, f.ID, f.Password)
		if err != nil {
			continue
		}
		out = append(out, map[string]any{"encrypted_path": it.EncryptedPath, "at": it.At, "size": info.Size,
			"name": info.Name, "modified": info.Modified.Format(time.RFC3339), "deleted": info.Deleted})
	}
	return out, nil
}

func (o *ops) stagingFile(folderID, encPath string) string {
	safe := strings.NewReplacer("/", "_", "\\", "_", "..", "_").Replace(encPath)
	return filepath.Join(o.s.Mgr.HeldPath("restore-tmp"), folderID, safe)
}

func (o *ops) RestoreVersionChunk(folderID, encPath string, offset int64, data []byte) error {
	if _, err := o.owned(folderID); err != nil {
		return err
	}
	p := o.stagingFile(folderID, encPath)
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if offset == 0 {
		_ = f.Truncate(0)
	}
	_, err = f.WriteAt(data, offset)
	return err
}

func (o *ops) RestoreVersionFinish(folderID, encPath, homeRoot string) (string, error) {
	f, err := o.owned(folderID)
	if err != nil {
		return "", err
	}
	p := o.stagingFile(folderID, encPath)
	data, err := os.ReadFile(p)
	if err != nil {
		return "", errors.New("nothing received for that version")
	}
	defer os.Remove(p)
	dest := filepath.Join(homeRoot, "Casper Restores", fmt.Sprintf("%s (versions, %s)", f.Label, time.Now().Format("2006-01-02 15.04.05")))
	return mirror.DecryptVersion(o.s.Bin, encPath, data, f.ID, f.Password, dest)
}

func (o *ops) UnprotectedFiles(folderID string) ([]string, error) {
	f, err := o.owned(folderID)
	if err != nil {
		return nil, err
	}
	st, _ := o.s.Mgr.OwnedStatus(f)
	return st.UnprotectedNames, nil
}
