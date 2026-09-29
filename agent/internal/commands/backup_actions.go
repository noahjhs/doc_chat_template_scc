package commands

import (
	"encoding/json"
	"errors"
	"path/filepath"

	"casper-agent/internal/backup"
)

// SetBackupStore gives this identity its peer-backup store (see
// cmd/casper/daemon.go). Until one is set, every backup action fails.
func (h *Handler) SetBackupStore(s *backup.Store) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.backup = s
}

func (h *Handler) backupStore() *backup.Store {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.backup
}

// RestoresDirName is where restores land, inside the confined home
// directory -- always a brand-new subfolder, never over anything existing.
const RestoresDirName = "Casper Restores"

// dispatchBackup runs one backup action. Results carry their data as JSON
// in Stdout; peer-side write decisions also set Tier, the same way
// run_shell_command reports its verdict. A backup.UserError (a refusal
// meant for a person) comes back as success=false with the message in
// Stderr, not as an HTTP error.
func (h *Handler) dispatchBackup(req *Request) (Result, error) {
	s := h.backupStore()
	if s == nil {
		return h.fail("Backups aren't set up on this host yet."), nil
	}
	out, tier, err := h.runBackupAction(s, req)
	if err != nil {
		var ue *backup.UserError
		if errors.As(err, &ue) {
			return h.fail(ue.Msg), nil
		}
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
	r := h.ok(string(data), "")
	r.Tier = tier
	if tier == "deny" || (tier == "ask" && !req.Approved) {
		r.Success = false
	}
	return r, nil
}

func (h *Handler) runBackupAction(s *backup.Store, req *Request) (map[string]any, string, error) {
	switch req.Action {
	// --- As the backup owner -------------------------------------------
	case "backup_key_info":
		keys, storage, err := s.Keys()
		if err != nil {
			return nil, "", err
		}
		return map[string]any{"signing_public_key": keys.SigningPublicKey(), "created_storage": storage}, "", nil
	case "backup_export_key":
		keys, _, err := s.Keys()
		if err != nil {
			return nil, "", err
		}
		armored, err := backup.ExportKeys(keys, req.Passphrase)
		if err != nil {
			return nil, "", &backup.UserError{Msg: err.Error()}
		}
		return map[string]any{"exported_key": armored}, "", nil
	case "backup_prepare":
		path, err := require(req.Path, "path", req.Action)
		if err != nil {
			return nil, "", err
		}
		dir, err := h.resolvePath(path, true, true)
		if err != nil {
			return nil, "", err
		}
		size, err := s.Prepare(req.BackupID, dir)
		if err != nil {
			return nil, "", err
		}
		return map[string]any{"backup_id": req.BackupID, "plaintext_bytes": size, "name": filepath.Base(dir)}, "", nil
	case "backup_prepare_status":
		out, err := s.PrepareStatus(req.BackupID)
		return out, "", err
	case "backup_read_chunk":
		c, err := s.ReadChunk(req.BackupID, req.Index)
		return map[string]any{"content": c}, "", err
	case "backup_cleanup":
		return map[string]any{}, "", s.Cleanup(req.BackupID)
	case "backup_restore_begin":
		return map[string]any{}, "", s.RestoreBegin(req.BackupID, req.Manifest, req.Signature)
	case "backup_restore_chunk":
		return map[string]any{}, "", s.RestoreChunk(req.BackupID, req.Index, req.Content)
	case "backup_unpack":
		target, err := s.Unpack(req.BackupID, filepath.Join(h.HomeRoot(), RestoresDirName))
		return map[string]any{"restored_to": target}, "", err

	// --- As the backup peer (on behalf of req.Grantee) ------------------
	case "backup_authorize":
		tier, reason, err := s.Authorize(req.Grantee, req.BackupID, req.TotalBytes)
		return map[string]any{"reason": reason}, tier, err
	case "backup_write_chunk":
		tier, reason, complete, err := s.WriteChunk(req.Grantee, req.BackupID, req.Index, req.Content, req.Manifest, req.Signature, req.Approved)
		return map[string]any{"reason": reason, "complete": complete}, tier, err
	case "backup_list":
		list, err := s.List(req.Grantee)
		return map[string]any{"backups": list}, "", err
	case "backup_get_manifest":
		m, sig, n, err := s.GetManifest(req.Grantee, req.BackupID)
		return map[string]any{"manifest": m, "signature": sig, "chunk_count": n}, "", err
	case "backup_get_chunk":
		c, err := s.GetChunk(req.Grantee, req.BackupID, req.Index)
		return map[string]any{"content": c}, "", err
	case "backup_delete":
		return map[string]any{}, "", s.Delete(req.Grantee, req.BackupID)
	case "refresh_grants":
		purged, err := s.RefreshGrants()
		return map[string]any{"purged": purged}, "", err
	default:
		return nil, "", &ActionError{Detail: "Action not authorized."}
	}
}
