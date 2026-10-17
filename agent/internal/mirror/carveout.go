package mirror

import (
	"net/url"
	"strings"
)

// Casper leaves the macOS Photos library out of mirroring for now
// (Principal's decision 2026-10-16): it is a huge package of databases the
// system guards, and it can't be mirrored safely file by file.
const (
	photosIgnore  = "(?i)*.photoslibrary"
	photosComment = "// Casper: the Photos library isn't mirrored yet"
)

// IsPhotosLibrary reports whether a file or folder name is a Photos library.
func IsPhotosLibrary(name string) bool {
	return strings.HasSuffix(strings.ToLower(name), ".photoslibrary")
}

// carveOut makes sure folderID's ignore list (Syncthing's .stignore, kept in
// the folder) carries the Photos library pattern, leaving any other line the
// person put there alone. A no-op once it's there.
func (m *Manager) carveOut(folderID, folderPath string) error {
	path := "/rest/db/ignores?folder=" + url.QueryEscape(folderID)
	var cur struct {
		Ignore []string `json:"ignore"`
	}
	if err := m.get(path, &cur); err != nil {
		return err
	}
	have := map[string]bool{}
	for _, l := range cur.Ignore {
		have[strings.TrimSpace(l)] = true
	}
	var add []string
	if !have[photosIgnore] {
		add = append(add, photosComment, photosIgnore)
	}
	// OneDrive placeholders: reading one downloads it, so they're left out
	// (and reported as unreadable, see cloudOnlyFiles) until it's on this PC.
	for _, rel := range m.cloudOnlyFiles(folderID, folderPath) {
		if line := ignoreLine(rel); !have[line] {
			add = append(add, line)
		}
	}
	if len(add) == 0 {
		return nil
	}
	return m.do("POST", path, map[string]any{"ignore": append(cur.Ignore, add...)}, nil)
}
