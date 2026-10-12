package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// Pairing by code: the app asks casper_service for a code, opens the
// sign-in page with it, and polls (with a secret only it knows) until the
// signed-in page approves -- then it pairs with the one-time token it gets
// back. No browser-to-app hand-off, so the browser never asks "Open Casper?".

func postPairing(authDomain, path string, body, out any) error {
	data, _ := json.Marshal(body)
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Post(BaseURL(authDomain)+path, "application/json", bytes.NewReader(data))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: HTTP %d", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// StartPairing gets a pairing code (for the sign-in link) and the secret
// that collects its result.
func StartPairing(authDomain string) (code, secret string, err error) {
	var out struct{ Code, Secret string }
	err = postPairing(authDomain, "/pairing/start", map[string]any{}, &out)
	return out.Code, out.Secret, err
}

// CodePairingStatus reports whether the code was approved, and if so the
// one-time token and username to pair with (handed out once).
func CodePairingStatus(authDomain, code, secret string) (approved bool, token, username string, err error) {
	var out struct {
		Approved bool   `json:"approved"`
		Token    string `json:"token"`
		Username string `json:"username"`
	}
	err = postPairing(authDomain, "/pairing/status", map[string]string{"code": code, "secret": secret}, &out)
	return out.Approved, out.Token, out.Username, err
}
