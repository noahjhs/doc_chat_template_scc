//go:build !windows

// Package instance: see instance_windows.go. Elsewhere the OS already keeps
// one app running, so every process is the first.
package instance

import "errors"

func Acquire(name string, onMessage func(string)) (bool, error) { return true, nil }

func Release() {}

func Send(name, message string) error { return errors.New("single instance is handled by the OS here") }
