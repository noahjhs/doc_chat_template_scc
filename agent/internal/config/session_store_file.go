//go:build !darwin && !windows

package config

func defaultSessionStore() sessionStore { return fileSessionStore{} }
