package mirror

import (
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// PeerState is how one mirror or catcher stands for an owned folder.
type PeerState struct {
	DeviceID   string  `json:"device_id"`
	Role       string  `json:"role"` // "mirror" | "catcher"
	Connected  bool    `json:"connected"`
	Completion float64 `json:"completion"`
	NeedFiles  int     `json:"need_files"`
	LastSeen   string  `json:"last_seen"`
}

// FolderStatus is an owned folder's protection, as Casper reports it
// (mirroring.md, "Protection status, precisely").
type FolderStatus struct {
	FolderID   string `json:"folder_id"`
	LocalFiles int    `json:"local_files"`
	LocalBytes int64  `json:"local_bytes"`
	// At risk: changed files no mirror has yet.
	AtRiskFiles int `json:"at_risk_files"`
	// Unprotected: at-risk files the catcher doesn't hold either -- the
	// honest number. Oldest is the oldest such change's modification time.
	UnprotectedFiles  int         `json:"unprotected_files"`
	UnprotectedOldest int64       `json:"unprotected_oldest_unix"`
	Peers             []PeerState `json:"peers"`
	Error             string      `json:"error,omitempty"`
	// UnprotectedNames lists (up to 50) unprotected files by name. Never
	// reported to casper_service: it's only sent, live, to the person's own
	// agent when they ask (mirror_unprotected_files).
	UnprotectedNames []string `json:"-"`
}

// needed returns the files (not deletions) device still needs in folder.
func (m *Manager) needed(folder, device string) (map[string]bool, error) {
	out := map[string]bool{}
	for page := 1; ; page++ {
		var resp struct {
			Files []struct {
				Name    string `json:"name"`
				Deleted bool   `json:"deleted"`
				Type    any    `json:"type"`
			} `json:"files"`
		}
		path := fmt.Sprintf("/rest/db/remoteneed?folder=%s&device=%s&page=%d&perpage=2000", url.QueryEscape(folder), url.QueryEscape(device), page)
		if err := m.get(path, &resp); err != nil {
			return nil, err
		}
		for _, f := range resp.Files {
			if !f.Deleted {
				out[f.Name] = true
			}
		}
		if len(resp.Files) < 2000 {
			return out, nil
		}
	}
}

func (m *Manager) peerState(folder, device, role string, conns map[string]bool, seen map[string]string) PeerState {
	ps := PeerState{DeviceID: device, Role: role, Connected: conns[device], LastSeen: seen[device]}
	var c struct {
		Completion float64 `json:"completion"`
		NeedItems  int     `json:"needItems"`
	}
	if err := m.get(fmt.Sprintf("/rest/db/completion?folder=%s&device=%s", url.QueryEscape(folder), url.QueryEscape(device)), &c); err == nil {
		ps.Completion, ps.NeedFiles = c.Completion, c.NeedItems
	}
	return ps
}

func (m *Manager) connections() (map[string]bool, map[string]string) {
	var conns struct {
		Connections map[string]struct {
			Connected bool `json:"connected"`
		} `json:"connections"`
	}
	_ = m.get("/rest/system/connections", &conns)
	connected := map[string]bool{}
	for id, c := range conns.Connections {
		connected[id] = c.Connected
	}
	var stats map[string]struct {
		LastSeen string `json:"lastSeen"`
	}
	_ = m.get("/rest/stats/device", &stats)
	seen := map[string]string{}
	for id, s := range stats {
		seen[id] = s.LastSeen
	}
	return connected, seen
}

// OwnedStatus computes protection for one owned folder, and returns the
// at-risk paths (for the catcher's staging area).
func (m *Manager) OwnedStatus(f OwnedFolder) (FolderStatus, []string) {
	st := FolderStatus{FolderID: f.ID}
	conns, seen := m.connections()
	var atRisk map[string]bool
	for i, dev := range f.Mirrors {
		st.Peers = append(st.Peers, m.peerState(f.ID, dev, "mirror", conns, seen))
		need, err := m.needed(f.ID, dev)
		if err != nil {
			st.Error = err.Error()
			return st, nil
		}
		if i == 0 {
			atRisk = need
			continue
		}
		for name := range atRisk { // at risk only if EVERY mirror still needs it
			if !need[name] {
				delete(atRisk, name)
			}
		}
	}
	files := listFiles(f.Path)
	st.LocalFiles = len(files)
	for _, rel := range files {
		if info, err := os.Stat(filepath.Join(f.Path, rel)); err == nil {
			st.LocalBytes += info.Size()
		}
	}
	if len(f.Mirrors) == 0 {
		atRisk = map[string]bool{}
		for _, rel := range files {
			atRisk[rel] = true
		}
	}
	risk := make([]string, 0, len(atRisk))
	for name := range atRisk {
		if _, err := os.Stat(filepath.Join(f.Path, name)); err == nil {
			risk = append(risk, name)
		}
	}
	sort.Strings(risk)
	st.AtRiskFiles = len(risk)

	caught := map[string]bool{}
	if f.Catcher != "" {
		st.Peers = append(st.Peers, m.peerState(CatchFolderID(f.ID), f.Catcher, "catcher", conns, seen))
		if need, err := m.needed(CatchFolderID(f.ID), f.Catcher); err == nil {
			for _, rel := range risk {
				if _, err := os.Stat(filepath.Join(m.StagingPath(f.ID), rel)); err == nil && !need[rel] && conns[f.Catcher] {
					caught[rel] = true
				}
			}
		}
	}
	for _, rel := range risk {
		if caught[rel] {
			continue
		}
		st.UnprotectedFiles++
		if len(st.UnprotectedNames) < 50 {
			st.UnprotectedNames = append(st.UnprotectedNames, rel)
		}
		if info, err := os.Stat(filepath.Join(f.Path, rel)); err == nil {
			if t := info.ModTime().Unix(); st.UnprotectedOldest == 0 || t < st.UnprotectedOldest {
				st.UnprotectedOldest = t
			}
		}
	}
	return st, risk
}

// listFiles returns regular files under root, relative, skipping
// Syncthing's own markers.
func listFiles(root string) []string {
	var out []string
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && (d.Name() == ".stfolder" || d.Name() == ".stversions") {
			return filepath.SkipDir
		}
		if d.Type().IsRegular() {
			rel, _ := filepath.Rel(root, p)
			if !strings.HasPrefix(rel, ".stignore") {
				out = append(out, filepath.ToSlash(rel))
			}
		}
		return nil
	})
	return out
}

// DirSize is the total size of regular files under root.
func DirSize(root string) int64 {
	var total int64
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				total += info.Size()
			}
		}
		return nil
	})
	return total
}

// FolderState is Syncthing's own view of one local folder: how many files
// it still needs and what it's doing ("idle", "syncing", ...). Used to
// report a restore's progress.
func (m *Manager) FolderState(folderID string) (needFiles int, state string, err error) {
	var st struct {
		NeedFiles int    `json:"needFiles"`
		State     string `json:"state"`
	}
	err = m.get("/rest/db/status?folder="+url.QueryEscape(folderID), &st)
	return st.NeedFiles, st.State, err
}
