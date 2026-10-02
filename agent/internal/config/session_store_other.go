//go:build !darwin

package config

func defaultSessionStore() sessionStore { return fileSessionStore{} }
