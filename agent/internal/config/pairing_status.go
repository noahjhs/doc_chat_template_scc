package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

// PairingStatus is the outcome of the most recent casper://pair attempt
// (see cmd/casper/daemon.go's handlePairURL), written to disk so a
// same-machine, out-of-process caller can read a definitive result --
// notably `harness pair-daemon`, which fires the pairing Apple Event and
// otherwise has no return channel back from it: a conflict today only
// ever surfaces as a native dialog.ShowError, meant for a real end user
// with no terminal, not for something a CLI run can observe or report.
type PairingStatus struct {
	Result  string    `json:"result"` // "ok", "conflict", or "error"
	Message string    `json:"message"`
	At      time.Time `json:"at"`
}

func pairingStatusFilePath() (string, error) {
	dir, err := AppConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "pairing_status.json"), nil
}

// SaveLastPairingResult is best-effort (mirrors SaveSession/ClearSession's
// posture elsewhere in this package) -- a failure to record the status is
// never worth failing pairing itself over; a harness caller that can't read
// it back just falls back to its own connected-host polling timeout.
func SaveLastPairingResult(result, message string) {
	path, err := pairingStatusFilePath()
	if err != nil {
		return
	}
	data, err := json.Marshal(PairingStatus{Result: result, Message: message, At: time.Now()})
	if err != nil {
		return
	}
	_ = os.WriteFile(path, data, 0o600)
}

// LoadLastPairingResult tolerates a missing or corrupt file (nil, nil in
// either case) -- same convention as LoadSession.
func LoadLastPairingResult() (*PairingStatus, error) {
	path, err := pairingStatusFilePath()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil //nolint:nilerr // missing file is a normal "no pairing attempt yet" case
	}
	var s PairingStatus
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, nil //nolint:nilerr // corrupt file is treated the same as no recorded status
	}
	return &s, nil
}
