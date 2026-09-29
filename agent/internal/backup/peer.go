package backup

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// --- Backup peer: store other accounts' opaque chunks under their grant ---
//
// The grantee is asserted by casper_service (the only party that can reach
// this daemon, via this identity's own command_key), exactly as trustworthy
// as every other field on a request. What the daemon does NOT take on faith
// is the grant itself: it fetches its own grant list from casper_service
// with its own device token, and decides every request from that list.

func (s *Store) currentGrants(forceRefresh bool) ([]Grant, error) {
	s.mu.Lock()
	fresh := s.grants != nil && s.now().Sub(s.grantsAt) < grantsMaxAge
	cached := s.grants
	s.mu.Unlock()
	if fresh && !forceRefresh {
		return cached, nil
	}
	if s.fetchGrants == nil {
		return nil, userErr("this host can't check its grants")
	}
	grants, err := s.fetchGrants()
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.grants, s.grantsAt = grants, s.now()
	s.mu.Unlock()
	return grants, nil
}

func findGrant(grants []Grant, grantee string) *Grant {
	for i := range grants {
		if grants[i].Grantee == grantee {
			return &grants[i]
		}
	}
	return nil
}

func (s *Store) peerDir(grantee string, parts ...string) string {
	return s.dir(append([]string{"peer", grantee}, parts...)...)
}

// usedBytes is how much of grantee's quota its stored backups take,
// excluding one backup (the one being written, which is counted at its
// manifest's full size by the caller instead).
func (s *Store) usedBytes(grantee, excluding string) int64 {
	entries, _ := os.ReadDir(s.peerDir(grantee))
	var used int64
	for _, e := range entries {
		if !e.IsDir() || e.Name() == excluding {
			continue
		}
		var sm signedManifest
		if readJSONFile(s.peerDir(grantee, e.Name(), "manifest.json"), &sm) != nil {
			continue
		}
		raw, err := base64.StdEncoding.DecodeString(sm.ManifestB64)
		if err != nil {
			continue
		}
		var m Manifest
		if jsonUnmarshal(raw, &m) == nil {
			used += m.TotalBytes
		}
	}
	return used
}

func checkGrantee(grantee string) error {
	if !usernamePattern.MatchString(grantee) {
		return userErr("invalid grantee")
	}
	return nil
}

// Authorize is the verdict for a hypothetical write of totalBytes: the
// grant's own write tier, or "deny" with a reason (no active grant, or over
// quota). Stores nothing. Always refetches grants -- this is the start of a
// backup, the moment a just-changed grant matters most.
func (s *Store) Authorize(grantee, backupID string, totalBytes int64) (tier, reason string, err error) {
	if err := checkGrantee(grantee); err != nil {
		return "", "", err
	}
	if err := checkID(backupID); err != nil {
		return "", "", err
	}
	grants, err := s.currentGrants(true)
	if err != nil {
		return "", "", err
	}
	return s.writeVerdict(grants, grantee, backupID, totalBytes)
}

func (s *Store) writeVerdict(grants []Grant, grantee, backupID string, totalBytes int64) (string, string, error) {
	g := findGrant(grants, grantee)
	if g == nil || g.RevokedAt != nil {
		return "deny", grantee + " has no backup space on this host", nil
	}
	if used := s.usedBytes(grantee, backupID); used+totalBytes > g.QuotaBytes {
		return "deny", "over quota: " + humanBytes(used) + " used + " + humanBytes(totalBytes) + " > " + humanBytes(g.QuotaBytes), nil
	}
	switch g.WriteTier {
	case "allow", "ask":
		return g.WriteTier, "", nil
	default:
		return "deny", "writes are switched off for " + grantee, nil
	}
}

// WriteChunk stores one chunk, deciding fresh every time: the grant must
// be active, the tier must allow it (an "ask" needs approved=true -- the
// daemon keeps no memory of an earlier verdict), the whole backup must fit
// the quota, and the chunk must match the manifest its owner signed. Chunk
// 0 carries that signed manifest. Returns the tier it decided.
func (s *Store) WriteChunk(grantee, backupID string, index int, contentB64, manifestB64, signatureB64 string, approved bool) (tier, reason string, complete bool, err error) {
	if err := checkGrantee(grantee); err != nil {
		return "", "", false, err
	}
	if err := checkID(backupID); err != nil {
		return "", "", false, err
	}
	grants, err := s.currentGrants(false)
	if err != nil {
		return "", "", false, err
	}
	g := findGrant(grants, grantee)
	if g == nil || g.RevokedAt != nil {
		return "deny", grantee + " has no backup space on this host", false, nil
	}
	dir := s.peerDir(grantee, backupID)

	var m Manifest
	if index == 0 {
		if manifestB64 == "" {
			return "deny", "chunk 0 must carry the backup's signed manifest", false, nil
		}
		m, err = verifyManifest(signedManifest{ManifestB64: manifestB64, SignatureB64: signatureB64}, g.SigningKeys)
		if err != nil {
			return "deny", err.Error(), false, nil
		}
		if m.BackupID != backupID {
			return "deny", "manifest is for a different backup", false, nil
		}
	} else {
		var sm signedManifest
		if err := readJSONFile(filepath.Join(dir, "manifest.json"), &sm); err != nil {
			return "deny", "chunk 0 hasn't been stored yet", false, nil
		}
		raw, _ := base64.StdEncoding.DecodeString(sm.ManifestB64)
		if err := jsonUnmarshal(raw, &m); err != nil {
			return "", "", false, err
		}
	}

	tier, reason, err = s.writeVerdict(grants, grantee, backupID, m.TotalBytes)
	if err != nil || tier == "deny" {
		return tier, reason, false, err
	}
	if tier == "ask" && !approved {
		return "ask", "", false, nil
	}

	if index < 0 || index >= len(m.ChunkSHA256) {
		return "", "", false, userErr("chunk index %d out of range", index)
	}
	data, err := base64.StdEncoding.DecodeString(contentB64)
	if err != nil {
		return "", "", false, userErr("invalid base64 content")
	}
	if sha256Hex(data) != m.ChunkSHA256[index] {
		return "deny", "chunk doesn't match the signed manifest", false, nil
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", "", false, err
	}
	if index == 0 {
		if err := writeJSONFile(filepath.Join(dir, "manifest.json"), signedManifest{ManifestB64: manifestB64, SignatureB64: signatureB64}); err != nil {
			return "", "", false, err
		}
	}
	if err := os.WriteFile(filepath.Join(dir, chunkName(index)), data, 0o600); err != nil {
		return "", "", false, err
	}
	complete = true
	for i := range m.ChunkSHA256 {
		if _, err := os.Stat(filepath.Join(dir, chunkName(i))); err != nil {
			complete = false
			break
		}
	}
	if complete {
		_ = os.WriteFile(filepath.Join(dir, "complete"), []byte(s.now().UTC().Format(time.RFC3339)), 0o600)
	}
	return tier, "", complete, nil
}

// readableBy reports whether grantee may still list/restore/delete what it
// stored: an active grant, or one revoked within RevocationGrace. Reading
// back your own blobs is authorized by owning them, not by the right to
// write more (docs/product/scenarios/peer-backup.md, step 9).
func (s *Store) readableBy(grantee string) (bool, error) {
	if err := checkGrantee(grantee); err != nil {
		return false, err
	}
	grants, err := s.currentGrants(false)
	if err != nil {
		return false, err
	}
	g := findGrant(grants, grantee)
	if g == nil {
		return false, nil
	}
	return g.RevokedAt == nil || s.now().Sub(*g.RevokedAt) < RevocationGrace, nil
}

// PeerBackup is one stored backup as its owner sees it -- sizes only.
type PeerBackup struct {
	BackupID   string `json:"backup_id"`
	TotalBytes int64  `json:"total_bytes"`
	Complete   bool   `json:"complete"`
	CreatedAt  string `json:"created_at"`
}

func (s *Store) List(grantee string) ([]PeerBackup, error) {
	ok, err := s.readableBy(grantee)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, userErr("%s has no backups on this host", grantee)
	}
	entries, _ := os.ReadDir(s.peerDir(grantee))
	out := []PeerBackup{}
	for _, e := range entries {
		var sm signedManifest
		if !e.IsDir() || readJSONFile(s.peerDir(grantee, e.Name(), "manifest.json"), &sm) != nil {
			continue
		}
		raw, _ := base64.StdEncoding.DecodeString(sm.ManifestB64)
		var m Manifest
		if jsonUnmarshal(raw, &m) != nil {
			continue
		}
		_, statErr := os.Stat(s.peerDir(grantee, e.Name(), "complete"))
		out = append(out, PeerBackup{BackupID: m.BackupID, TotalBytes: m.TotalBytes, Complete: statErr == nil, CreatedAt: m.CreatedAt})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt < out[j].CreatedAt })
	return out, nil
}

// GetManifest returns a stored backup's signed manifest, for a restore.
func (s *Store) GetManifest(grantee, backupID string) (manifestB64, signatureB64 string, chunkCount int, err error) {
	if err := checkID(backupID); err != nil {
		return "", "", 0, err
	}
	ok, err := s.readableBy(grantee)
	if err != nil {
		return "", "", 0, err
	}
	var sm signedManifest
	if !ok || readJSONFile(s.peerDir(grantee, backupID, "manifest.json"), &sm) != nil {
		return "", "", 0, userErr("no backup %s from %s on this host", backupID, grantee)
	}
	if _, err := os.Stat(s.peerDir(grantee, backupID, "complete")); err != nil {
		return "", "", 0, userErr("backup %s never finished uploading", backupID)
	}
	raw, _ := base64.StdEncoding.DecodeString(sm.ManifestB64)
	var m Manifest
	if err := jsonUnmarshal(raw, &m); err != nil {
		return "", "", 0, err
	}
	return sm.ManifestB64, sm.SignatureB64, len(m.ChunkSHA256), nil
}

func (s *Store) GetChunk(grantee, backupID string, index int) (string, error) {
	if err := checkID(backupID); err != nil {
		return "", err
	}
	ok, err := s.readableBy(grantee)
	if err != nil {
		return "", err
	}
	if !ok {
		return "", userErr("no backup %s from %s on this host", backupID, grantee)
	}
	data, err := os.ReadFile(s.peerDir(grantee, backupID, chunkName(index)))
	if err != nil {
		return "", userErr("chunk %d of backup %s isn't stored here", index, backupID)
	}
	return base64.StdEncoding.EncodeToString(data), nil
}

func (s *Store) Delete(grantee, backupID string) error {
	if err := checkID(backupID); err != nil {
		return err
	}
	ok, err := s.readableBy(grantee)
	if err != nil {
		return err
	}
	if !ok {
		return userErr("no backup %s from %s on this host", backupID, grantee)
	}
	return os.RemoveAll(s.peerDir(grantee, backupID))
}

// RefreshGrants refetches grants now (casper_service calls this right
// after any grant changes) and purges the stored backups of grantees whose
// grant was revoked more than RevocationGrace ago. A grantee missing from
// the list entirely is left alone -- only an explicit, expired revocation
// ever deletes anyone's data.
func (s *Store) RefreshGrants() (purged []string, err error) {
	grants, err := s.currentGrants(true)
	if err != nil {
		return nil, err
	}
	for _, g := range grants {
		if g.RevokedAt != nil && s.now().Sub(*g.RevokedAt) >= RevocationGrace && usernamePattern.MatchString(g.Grantee) {
			if _, err := os.Stat(s.peerDir(g.Grantee)); err == nil {
				if err := os.RemoveAll(s.peerDir(g.Grantee)); err == nil {
					purged = append(purged, g.Grantee)
				}
			}
		}
	}
	return purged, nil
}
