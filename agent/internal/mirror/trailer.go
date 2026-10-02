package mirror

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/syncthing/syncthing/lib/protocol"
	"google.golang.org/protobuf/proto"
)

// ReadTrailer returns an encrypted file's metadata trailer: the encrypted
// FileInfo Syncthing appends to every file it stores for an untrusted
// device, followed by its 4-byte length. The mirror sends only this (a few
// hundred bytes) so the owner can tell which file a version is, without
// fetching the version itself. Same layout Syncthing's own `decrypt`
// command reads.
func ReadTrailer(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	if _, err := f.Seek(-4, io.SeekEnd); err != nil {
		return nil, err
	}
	var bs [4]byte
	if _, err := io.ReadFull(f, bs[:]); err != nil {
		return nil, err
	}
	size := int64(binary.BigEndian.Uint32(bs[:]))
	if size <= 0 || size > 16<<20 {
		return nil, errors.New("implausible trailer size")
	}
	if _, err := f.Seek(-(4 + size), io.SeekEnd); err != nil {
		return nil, err
	}
	trailer := make([]byte, size)
	_, err = io.ReadFull(f, trailer)
	return trailer, err
}

// DecryptedInfo is what the owner learns from a trailer.
type DecryptedInfo struct {
	Name     string    `json:"name"`
	Size     int64     `json:"size"`
	Modified time.Time `json:"modified"`
	Deleted  bool      `json:"deleted"`
}

// DecryptTrailer decrypts a trailer with the folder's password, using
// Syncthing's own exported functions (never a reimplementation of its
// cryptography). Only the owner, who holds the password, can do this.
func DecryptTrailer(trailer []byte, folderID, password string) (DecryptedInfo, error) {
	// The wire type lives in a Syncthing-internal package we can't import,
	// so take a fresh (empty) one from the exported ToWire and fill it.
	encFi := (&protocol.FileInfo{}).ToWire(false)
	proto.Reset(encFi)
	if err := proto.Unmarshal(trailer, encFi); err != nil {
		return DecryptedInfo{}, err
	}
	fi := protocol.FileInfoFromWire(encFi)
	keyGen := protocol.NewKeyGenerator()
	folderKey := keyGen.KeyFromPassword(folderID, password)
	dec, err := protocol.DecryptFileInfo(keyGen, fi, folderKey)
	if err != nil {
		return DecryptedInfo{}, err
	}
	return DecryptedInfo{Name: dec.Name, Size: dec.Size, Modified: dec.ModTime(), Deleted: dec.IsDeleted()}, nil
}

// DecryptVersion turns one stored version (its encrypted bytes, as fetched
// from a mirror) back into the original file under destDir, using the
// bundled `syncthing decrypt`. encryptedRel is the version's encrypted path
// without its ~timestamp suffix: Syncthing's decrypt skips suffixed names
// (the spike's one gap), so the file is laid out as if it were current.
// Returns the restored file's path.
func DecryptVersion(bin, encryptedRel string, data []byte, folderID, password, destDir string) (string, error) {
	tmp, err := os.MkdirTemp("", "casper-version-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmp)
	clean := filepath.Clean("/" + filepath.FromSlash(encryptedRel))[1:]
	if clean == "" || strings.HasPrefix(clean, "..") {
		return "", errors.New("bad version path")
	}
	enc := filepath.Join(tmp, clean)
	if err := os.MkdirAll(filepath.Dir(enc), 0o700); err != nil {
		return "", err
	}
	if err := os.WriteFile(enc, data, 0o600); err != nil {
		return "", err
	}
	info, err := DecryptTrailer(data[len(data)-int(binary.BigEndian.Uint32(data[len(data)-4:]))-4:len(data)-4], folderID, password)
	if err != nil {
		return "", fmt.Errorf("this isn't a version of a folder you own (or the password is wrong): %w", err)
	}
	if out, err := exec.Command(bin, "decrypt", tmp, "--password="+password, "--folder-id="+folderID, "--to="+destDir).CombinedOutput(); err != nil {
		return "", fmt.Errorf("syncthing decrypt: %v: %s", err, out)
	}
	return filepath.Join(destDir, filepath.FromSlash(info.Name)), nil
}
