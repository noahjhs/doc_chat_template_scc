package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"casper-agent/internal/backup"
)

// FetchGrants GETs the Backup Peer grants other accounts hold on THIS
// identity's host, authenticated with this identity's own device token --
// see casper_service/main.py's GET /hosts/grants. Same posture as
// FetchPolicyLayers: the daemon fetches its own grants and decides every
// peer-side backup request from them, rather than trusting a grant
// asserted on the request.
func FetchGrants(authDomain, deviceToken string) ([]backup.Grant, error) {
	req, err := http.NewRequest(http.MethodGet, BaseURL(authDomain)+"/hosts/grants", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status %d fetching grants", resp.StatusCode)
	}
	var body struct {
		Grants []backup.Grant `json:"grants"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	return body.Grants, nil
}

// RegisterBackupKey POSTs this identity's backup signing public key, so
// peers storing its backups can verify it signed them (see
// casper_service/main.py's POST /hosts/backup-key). keyStorage is where the
// private keys live ("icloud"/"local"), shown to the owner.
func RegisterBackupKey(authDomain, deviceToken, signingPublicKey, keyStorage string) error {
	payload, _ := json.Marshal(map[string]string{"signing_public_key": signingPublicKey, "key_storage": keyStorage})
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/hosts/backup-key", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status %d registering backup key", resp.StatusCode)
	}
	return nil
}
