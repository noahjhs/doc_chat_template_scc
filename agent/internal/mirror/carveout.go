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
func (m *Manager) carveOut(folderID string) error {
	path := "/rest/db/ignores?folder=" + url.QueryEscape(folderID)
	var cur struct {
		Ignore []string `json:"ignore"`
	}
	if err := m.get(path, &cur); err != nil {
		return err
	}
	for _, l := range cur.Ignore {
		if strings.TrimSpace(l) == photosIgnore {
			return nil
		}
	}
	return m.do("POST", path, map[string]any{"ignore": append(cur.Ignore, photosComment, photosIgnore)}, nil)
}
