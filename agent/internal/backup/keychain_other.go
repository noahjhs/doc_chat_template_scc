//go:build !darwin

package backup

import (
	"errors"
)

// MacKeychain has no non-macOS implementation yet -- peer backup is
// macOS-only for the alpha (docs/product/v1-implementation-plan.md).
type MacKeychain struct{}

func (MacKeychain) Load(string) ([]byte, error) { return nil, errors.New("backup keys are macOS-only for now") }

func (MacKeychain) Save(string, []byte) (string, error) {
	return "", errors.New("backup keys are macOS-only for now")
}
