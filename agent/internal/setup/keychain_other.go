//go:build !darwin

package setup

import "errors"

func keychainLoad(string) ([]byte, error) { return nil, errors.New("setup is macOS-only for now") }

func keychainSave(string, []byte) error { return errors.New("setup is macOS-only for now") }
