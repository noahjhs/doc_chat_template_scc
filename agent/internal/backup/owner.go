package backup

import (
	"archive/tar"
	"encoding/base64"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"filippo.io/age"
)

// --- Backup owner: prepare (tar + encrypt + chunk + sign) -------------------

type prepareState struct {
	state    string // "preparing" | "ready" | "failed"
	err      string
	manifest signedManifest
	total    int64
	chunks   int
}

// PlaintextSize walks dir the way prepare will (regular files only) and
// returns the total bytes -- fast, so casper_service can know the size
// (quota checks, the approval message) before encryption finishes.
func PlaintextSize(dir string) (int64, error) {
	var total int64
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type().IsRegular() {
			info, err := d.Info()
			if err != nil {
				return err
			}
			total += info.Size()
		}
		return nil
	})
	return total, err
}

// Prepare starts encrypting dir (already resolved and confined by the
// caller) in the background and returns its plaintext size immediately.
// Symlinks and special files are skipped, not followed: a backup never
// reaches outside the folder it was asked for.
func (s *Store) Prepare(backupID, dir string) (int64, error) {
	if err := checkID(backupID); err != nil {
		return 0, err
	}
	keys, _, err := s.Keys()
	if err != nil {
		return 0, err
	}
	size, err := PlaintextSize(dir)
	if err != nil {
		return 0, err
	}
	if size > MaxBackupBytes {
		return 0, userErr("folder is %s; the alpha limit is %s per backup", humanBytes(size), humanBytes(MaxBackupBytes))
	}
	s.mu.Lock()
	if _, exists := s.prepares[backupID]; exists {
		s.mu.Unlock()
		return 0, userErr("backup %s is already being prepared", backupID)
	}
	st := &prepareState{state: "preparing"}
	s.prepares[backupID] = st
	s.mu.Unlock()

	go func() {
		sm, total, chunks, err := s.encryptToChunks(backupID, dir, keys)
		s.mu.Lock()
		defer s.mu.Unlock()
		if err != nil {
			st.state, st.err = "failed", err.Error()
			_ = os.RemoveAll(s.dir("staging", backupID))
			return
		}
		st.state, st.manifest, st.total, st.chunks = "ready", sm, total, chunks
	}()
	return size, nil
}

// chunkWriter splits a byte stream into ChunkSize files, hashing each.
type chunkWriter struct {
	dir    string
	buf    []byte
	hashes []string
	total  int64
}

func (w *chunkWriter) Write(p []byte) (int, error) {
	n := len(p)
	for len(p) > 0 {
		room := ChunkSize - len(w.buf)
		take := min(room, len(p))
		w.buf = append(w.buf, p[:take]...)
		p = p[take:]
		if len(w.buf) == ChunkSize {
			if err := w.flush(); err != nil {
				return 0, err
			}
		}
	}
	return n, nil
}

func (w *chunkWriter) flush() error {
	if len(w.buf) == 0 {
		return nil
	}
	if err := os.WriteFile(filepath.Join(w.dir, chunkName(len(w.hashes))), w.buf, 0o600); err != nil {
		return err
	}
	w.hashes = append(w.hashes, sha256Hex(w.buf))
	w.total += int64(len(w.buf))
	w.buf = w.buf[:0]
	return nil
}

func (s *Store) encryptToChunks(backupID, dir string, keys *Keys) (signedManifest, int64, int, error) {
	staging := s.dir("staging", backupID)
	if err := os.MkdirAll(staging, 0o700); err != nil {
		return signedManifest{}, 0, 0, err
	}
	cw := &chunkWriter{dir: staging, buf: make([]byte, 0, ChunkSize)}
	enc, err := age.Encrypt(cw, keys.Age.Recipient())
	if err != nil {
		return signedManifest{}, 0, 0, err
	}
	if err := writeTar(enc, dir); err != nil {
		return signedManifest{}, 0, 0, err
	}
	if err := enc.Close(); err != nil {
		return signedManifest{}, 0, 0, err
	}
	if err := cw.flush(); err != nil {
		return signedManifest{}, 0, 0, err
	}
	m := Manifest{
		Version:          1,
		BackupID:         backupID,
		CreatedAt:        s.now().UTC().Format(time.RFC3339),
		TotalBytes:       cw.total,
		ChunkSHA256:      cw.hashes,
		SigningPublicKey: keys.SigningPublicKey(),
	}
	sm, err := signManifest(m, keys.Signing)
	if err != nil {
		return signedManifest{}, 0, 0, err
	}
	return sm, cw.total, len(cw.hashes), nil
}

// writeTar archives dir's regular files and directories, every entry name
// prefixed with dir's own base name (so a restore knows what to call it --
// that name exists only inside the ciphertext).
func writeTar(w io.Writer, dir string) error {
	tw := tar.NewWriter(w)
	base := filepath.Base(dir)
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && !d.Type().IsRegular() {
			return nil // symlinks, sockets, devices: skipped, never followed
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		hdr, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(filepath.Join(base, rel))
		if d.IsDir() {
			hdr.Name += "/"
		}
		hdr.Uname, hdr.Gname, hdr.Uid, hdr.Gid = "", "", 0, 0
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		f, err := os.Open(p)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(tw, f)
		return err
	})
	if err != nil {
		return err
	}
	return tw.Close()
}

// PrepareStatus reports on a Prepare. Once "ready", the signed manifest is
// included -- casper_service hands it to the peer with chunk 0.
func (s *Store) PrepareStatus(backupID string) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.prepares[backupID]
	if !ok {
		return nil, userErr("no backup %s is being prepared on this host", backupID)
	}
	out := map[string]any{"state": st.state}
	switch st.state {
	case "failed":
		out["error"] = st.err
	case "ready":
		out["manifest"] = st.manifest.ManifestB64
		out["signature"] = st.manifest.SignatureB64
		out["total_bytes"] = st.total
		out["chunk_count"] = st.chunks
	}
	return out, nil
}

// ReadChunk returns one staged ciphertext chunk, base64.
func (s *Store) ReadChunk(backupID string, index int) (string, error) {
	if err := checkID(backupID); err != nil {
		return "", err
	}
	data, err := os.ReadFile(s.dir("staging", backupID, chunkName(index)))
	if err != nil {
		return "", userErr("chunk %d of backup %s isn't staged here", index, backupID)
	}
	return base64.StdEncoding.EncodeToString(data), nil
}

// Cleanup drops a finished (or abandoned) backup's local staging copy.
func (s *Store) Cleanup(backupID string) error {
	if err := checkID(backupID); err != nil {
		return err
	}
	s.mu.Lock()
	delete(s.prepares, backupID)
	s.mu.Unlock()
	return os.RemoveAll(s.dir("staging", backupID))
}

// --- Backup owner: restore (verify + reassemble + decrypt) ------------------

// RestoreBegin accepts a backup's signed manifest for restoring here. It
// must have been signed by THIS identity's own key -- only the host
// holding the key that made a backup can decrypt it.
func (s *Store) RestoreBegin(backupID, manifestB64, signatureB64 string) error {
	if err := checkID(backupID); err != nil {
		return err
	}
	keys, _, err := s.Keys()
	if err != nil {
		return err
	}
	sm := signedManifest{ManifestB64: manifestB64, SignatureB64: signatureB64}
	m, err := verifyManifest(sm, []string{keys.SigningPublicKey()})
	if err != nil {
		return userErr("this backup was made with a different key than this host's -- restore it on the machine that made it (or import that machine's exported keys)")
	}
	if m.BackupID != backupID {
		return userErr("manifest is for a different backup")
	}
	dir := s.dir("restore", backupID)
	_ = os.RemoveAll(dir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return writeJSONFile(filepath.Join(dir, "manifest.json"), sm)
}

func (s *Store) restoreManifest(backupID string) (Manifest, error) {
	var sm signedManifest
	if err := readJSONFile(s.dir("restore", backupID, "manifest.json"), &sm); err != nil {
		return Manifest{}, userErr("restore of %s hasn't been started here", backupID)
	}
	keys, _, err := s.Keys()
	if err != nil {
		return Manifest{}, err
	}
	return verifyManifest(sm, []string{keys.SigningPublicKey()})
}

// RestoreChunk stores one chunk for a restore, refusing anything whose hash
// doesn't match the (signed) manifest.
func (s *Store) RestoreChunk(backupID string, index int, contentB64 string) error {
	if err := checkID(backupID); err != nil {
		return err
	}
	m, err := s.restoreManifest(backupID)
	if err != nil {
		return err
	}
	if index < 0 || index >= len(m.ChunkSHA256) {
		return userErr("chunk index %d out of range", index)
	}
	data, err := base64.StdEncoding.DecodeString(contentB64)
	if err != nil {
		return userErr("invalid base64 content")
	}
	if sha256Hex(data) != m.ChunkSHA256[index] {
		return userErr("chunk %d doesn't match the backup's manifest -- it was altered or corrupted", index)
	}
	return os.WriteFile(s.dir("restore", backupID, chunkName(index)), data, 0o600)
}

// Unpack decrypts a fully received restore into a new folder under
// restoresRoot, named after the original folder plus a timestamp. Never
// overwrites: the target folder is always new. Returns its path.
func (s *Store) Unpack(backupID, restoresRoot string) (string, error) {
	if err := checkID(backupID); err != nil {
		return "", err
	}
	m, err := s.restoreManifest(backupID)
	if err != nil {
		return "", err
	}
	keys, _, err := s.Keys()
	if err != nil {
		return "", err
	}
	dir := s.dir("restore", backupID)
	for i := range m.ChunkSHA256 {
		if _, err := os.Stat(filepath.Join(dir, chunkName(i))); err != nil {
			return "", userErr("chunk %d hasn't arrived yet", i)
		}
	}
	chunks := &chunkReader{dir: dir, count: len(m.ChunkSHA256)}
	defer chunks.Close()
	plain, err := age.Decrypt(chunks, keys.Age)
	if err != nil {
		return "", fmt.Errorf("decrypting: %w", err)
	}
	stamp := s.now().Format("20060102-150405")
	target, err := extractTar(plain, restoresRoot, stamp)
	if err != nil {
		return "", err
	}
	_ = os.RemoveAll(dir)
	return target, nil
}

// chunkReader reads chunk files back as one stream, opening one at a time
// (a 2GB backup is 512 chunks -- more than a default open-file limit).
type chunkReader struct {
	dir   string
	count int
	next  int
	cur   *os.File
}

func (c *chunkReader) Read(p []byte) (int, error) {
	for {
		if c.cur == nil {
			if c.next >= c.count {
				return 0, io.EOF
			}
			f, err := os.Open(filepath.Join(c.dir, chunkName(c.next)))
			if err != nil {
				return 0, err
			}
			c.cur, c.next = f, c.next+1
		}
		n, err := c.cur.Read(p)
		if err == io.EOF {
			c.cur.Close()
			c.cur = nil
			if n > 0 {
				return n, nil
			}
			continue
		}
		return n, err
	}
}

func (c *chunkReader) Close() {
	if c.cur != nil {
		c.cur.Close()
	}
}

// extractTar writes entries under a brand-new <restoresRoot>/<name>-<stamp>
// folder, where name is the archive's own top-level folder. Rejects any
// entry that would land outside that folder, and anything that isn't a
// plain file or directory.
func extractTar(r io.Reader, restoresRoot, stamp string) (string, error) {
	tr := tar.NewReader(r)
	var target string
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", fmt.Errorf("reading archive: %w", err)
		}
		clean := filepath.Clean(filepath.FromSlash(hdr.Name))
		parts := strings.SplitN(clean, string(filepath.Separator), 2)
		if filepath.IsAbs(clean) || parts[0] == ".." || parts[0] == "." || strings.Contains(clean, ".."+string(filepath.Separator)) {
			return "", fmt.Errorf("archive entry %q escapes the restore folder", hdr.Name)
		}
		if target == "" {
			target = filepath.Join(restoresRoot, fmt.Sprintf("%s-%s", parts[0], stamp))
			if _, err := os.Stat(target); err == nil {
				return "", fmt.Errorf("%s already exists", target)
			}
			if err := os.MkdirAll(target, 0o755); err != nil {
				return "", err
			}
		}
		rel := ""
		if len(parts) == 2 {
			rel = parts[1]
		}
		dest := filepath.Join(target, rel)
		if dest != target && !strings.HasPrefix(dest, target+string(filepath.Separator)) {
			return "", fmt.Errorf("archive entry %q escapes the restore folder", hdr.Name)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(dest, 0o755); err != nil {
				return "", err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
				return "", err
			}
			f, err := os.OpenFile(dest, os.O_CREATE|os.O_EXCL|os.O_WRONLY, os.FileMode(hdr.Mode).Perm()|0o600)
			if err != nil {
				return "", err
			}
			if _, err := io.Copy(f, tr); err != nil {
				f.Close()
				return "", err
			}
			if err := f.Close(); err != nil {
				return "", err
			}
			_ = os.Chtimes(dest, hdr.ModTime, hdr.ModTime)
		default:
			// Never produced by writeTar; ignored rather than trusted.
		}
	}
	if target == "" {
		return "", fmt.Errorf("the backup was empty")
	}
	return target, nil
}

func humanBytes(n int64) string {
	const unit = 1024
	if n < unit {
		return fmt.Sprintf("%d B", n)
	}
	div, exp := int64(unit), 0
	for m := n / unit; m >= unit; m /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %cB", float64(n)/float64(div), "KMGTPE"[exp])
}
