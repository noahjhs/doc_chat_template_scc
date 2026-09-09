package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"time"
)

// ErrHostConflict is returned by ExchangePairingToken when this host's
// routing_key is currently attached to a different account -- the auth
// service rejects rather than preempts an existing session (see
// auth_service/main.py's _pair_host / HostAlreadyAttachedError).
var ErrHostConflict = errors.New("this host is currently attached to another account")

// ExchangePairingToken trades a one-time browser bootstrap token (carried by
// a casper://pair URL) for this installation's own independent
// device_token/command_key, tied to its stable, self-persisted routing key
// (see routingkey.go). Called once per pairing; from then on the daemon
// never touches the browser's token again, so a future website login can't
// disturb an already-attached daemon. os.Hostname() is best-effort/cosmetic
// only -- a blank value just means the paired host shows up unnamed until
// the user gives it a label.
func ExchangePairingToken(authDomain, bootstrapToken, routingKey string) (deviceToken, commandKey, label string, err error) {
	hostname, _ := os.Hostname()

	body, err := json.Marshal(map[string]string{
		"routing_key": routingKey,
		"hostname":    hostname,
	})
	if err != nil {
		return "", "", "", err
	}
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/hosts/pair", bytes.NewReader(body))
	if err != nil {
		return "", "", "", err
	}
	req.Header.Set("Authorization", "Bearer "+bootstrapToken)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusConflict {
		return "", "", "", ErrHostConflict
	}
	if resp.StatusCode != http.StatusCreated {
		return "", "", "", fmt.Errorf("pairing failed: auth service returned %d", resp.StatusCode)
	}
	var out struct {
		DeviceToken string `json:"device_token"`
		CommandKey  string `json:"command_key"`
		Label       string `json:"label"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", "", "", err
	}
	return out.DeviceToken, out.CommandKey, out.Label, nil
}

// VerifyHostSession POSTs to the auth service's /hosts/verify endpoint --
// mirrors the old VerifySession, but checks a device_token's attachment
// (in-memory, transient) rather than a browser login token.
func VerifyHostSession(authDomain, deviceToken string) VerifyResult {
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/hosts/verify", nil)
	if err != nil {
		return VerifyUnknown
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return VerifyUnknown
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return VerifyUnknown
	}
	var body struct {
		Valid bool `json:"valid"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return VerifyUnknown
	}
	if body.Valid {
		return VerifyValid
	}
	return VerifyInvalid
}

// UnpairHost POSTs to the auth service's /hosts/unpair endpoint -- mirrors
// the old RevokeSession. Self-service deregistration: clears this host's
// attachment, but never touches the user's remembered relationship to it
// (see auth_service/main.py's unpair_host).
func UnpairHost(authDomain, deviceToken string) {
	req, err := http.NewRequest(http.MethodPost, BaseURL(authDomain)+"/hosts/unpair", bytes.NewReader(nil))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err == nil {
		resp.Body.Close()
	}
}
