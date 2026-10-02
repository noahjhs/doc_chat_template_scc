package commands

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"time"
)

// MirrorOps is what one paired identity's mirroring can do on this Mac
// (implemented by internal/mirrord). Every method is scoped to that
// identity: owned-folder methods only work on folders it owns, held-folder
// methods only on folders it holds for others under its own grants.
type MirrorOps interface {
	Refresh()
	AskConsent(approvalID, text string)
	VersionTrailers(folderID string) ([]map[string]any, error)
	ReadVersion(folderID, encryptedPath string, at time.Time, offset int64) (data []byte, total int64, err error)
	DecryptTrailers(folderID string, items json.RawMessage) ([]map[string]any, error)
	RestoreVersionChunk(folderID, encryptedPath string, offset int64, data []byte) error
	RestoreVersionFinish(folderID, encryptedPath string, restoresRoot string) (string, error)
}

func (h *Handler) SetMirrorOps(m MirrorOps) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.mirror = m
}

func (h *Handler) mirrorOps() MirrorOps {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.mirror
}

func (h *Handler) dispatchMirror(req *Request) (Result, error) {
	m := h.mirrorOps()
	if m == nil {
		return h.fail("Mirroring isn't set up on this host."), nil
	}
	out, err := h.runMirrorAction(m, req)
	if err != nil {
		var ae *ActionError
		if errors.As(err, &ae) {
			return Result{}, err
		}
		return h.fail(err.Error()), nil
	}
	data, err := json.Marshal(out)
	if err != nil {
		return Result{}, err
	}
	return h.ok(string(data), ""), nil
}

func (h *Handler) runMirrorAction(m MirrorOps, req *Request) (map[string]any, error) {
	switch req.Action {
	case "mirror_refresh":
		m.Refresh()
		return map[string]any{}, nil
	case "mirror_folder_size":
		// The folder to be mirrored, resolved and confined like any other
		// path this daemon touches.
		path, err := require(req.Path, "path", req.Action)
		if err != nil {
			return nil, err
		}
		dir, err := h.resolvePath(path, true, true)
		if err != nil {
			return nil, err
		}
		return map[string]any{"path": dir, "bytes": dirBytes(dir)}, nil
	case "ask_consent":
		m.AskConsent(req.ApprovalID, req.Text)
		return map[string]any{"asked": true}, nil
	case "mirror_version_trailers":
		items, err := m.VersionTrailers(req.FolderID)
		return map[string]any{"versions": items}, err
	case "mirror_read_version":
		at, err := time.Parse(time.RFC3339, req.At)
		if err != nil {
			return nil, &ActionError{Detail: "'at' must be RFC 3339"}
		}
		data, total, err := m.ReadVersion(req.FolderID, req.EncryptedPath, at, req.Offset)
		return map[string]any{"content": base64.StdEncoding.EncodeToString(data), "total": total}, err
	case "mirror_decrypt_trailers":
		items, err := m.DecryptTrailers(req.FolderID, req.Items)
		return map[string]any{"versions": items}, err
	case "mirror_restore_version_chunk":
		data, err := base64.StdEncoding.DecodeString(req.Content)
		if err != nil {
			return nil, &ActionError{Detail: "invalid base64 content"}
		}
		return map[string]any{}, m.RestoreVersionChunk(req.FolderID, req.EncryptedPath, req.Offset, data)
	case "mirror_restore_version_finish":
		path, err := m.RestoreVersionFinish(req.FolderID, req.EncryptedPath, h.HomeRoot())
		return map[string]any{"restored_to": path}, err
	default:
		return nil, &ActionError{Detail: "Action not authorized."}
	}
}

func dirBytes(root string) int64 {
	var total int64
	_ = walkRegular(root, func(size int64) { total += size })
	return total
}
