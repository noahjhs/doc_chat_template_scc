package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Mirroring calls to casper_service, each authenticated with one paired
// identity's device token (see internal/mirrord). casper_service is the
// discovery service (this Mac's Syncthing device ID and addresses) and the
// source of the desired mirroring state computed from that identity's
// grants -- the daemon never accepts a peer or folder it wasn't told about.

func deviceCall(method, authDomain, deviceToken, path string, body any, out any) error {
	var rd *bytes.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	} else {
		rd = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, BaseURL(authDomain)+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+deviceToken)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("unexpected status %d from %s", resp.StatusCode, path)
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

func ReportMirrorDevice(authDomain, deviceToken, deviceID string, addresses []string) error {
	return deviceCall("POST", authDomain, deviceToken, "/hosts/mirror-device", map[string]any{"device_id": deviceID, "addresses": addresses}, nil)
}

// FetchMirrorConfig decodes casper_service's desired state into out (a
// *mirror.Desired; kept as `any` so this package needn't import mirror).
func FetchMirrorConfig(authDomain, deviceToken string, out any) error {
	return deviceCall("GET", authDomain, deviceToken, "/hosts/mirror-config", nil, out)
}

func ReportMirrorStatus(authDomain, deviceToken string, folders any) error {
	return deviceCall("POST", authDomain, deviceToken, "/hosts/mirror-status", map[string]any{"folders": folders}, nil)
}

// PostConsent records the person's answer from the native consent dialog.
func PostConsent(authDomain, deviceToken, approvalID string, approve bool) error {
	return deviceCall("POST", authDomain, deviceToken, "/hosts/consent", map[string]any{"approval_id": approvalID, "approve": approve}, nil)
}
