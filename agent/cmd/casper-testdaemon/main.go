// Command casper-testdaemon is a headless stand-in for the real Casper.app,
// for integration tests only (tests/test_backup_integration.py) -- never
// built into a release. It runs the REAL daemon HTTP server, command
// handler and backup store for one already-paired identity, so a test can
// drive real encryption, signing, grant enforcement and restore through a
// real casper_service -- everything except the menu bar, the Keychain (an
// in-memory key store instead) and the relay (casper_service reaches it
// directly on localhost).
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"

	"casper-agent/internal/backup"
	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/server"
)

func main() {
	port := flag.Int("port", 0, "port to listen on")
	authDomain := flag.String("auth-domain", "", "casper_service host:port")
	username := flag.String("username", "", "the paired account's username")
	deviceToken := flag.String("device-token", "", "the pairing's device token")
	commandKey := flag.String("command-key", "", "the pairing's command key")
	home := flag.String("home", "", "the confined home directory")
	data := flag.String("data", "", "where backups/keys state lives")
	flag.Parse()

	logger := log.New(os.Stderr, "testdaemon "+*username+": ", log.LstdFlags)
	handler := commands.New(*home)
	store := backup.NewStore(filepath.Join(*data, "backups", *username), backup.NewMemoryKeyStore(), *username,
		func() ([]backup.Grant, error) { return config.FetchGrants(*authDomain, *deviceToken) })
	handler.SetBackupStore(store)

	keys, _, err := store.Keys()
	if err != nil {
		logger.Fatal(err)
	}
	if err := config.RegisterBackupKey(*authDomain, *deviceToken, keys.SigningPublicKey(), "memory"); err != nil {
		logger.Fatal(err)
	}

	srv := server.New(logger)
	srv.AddIdentity(*commandKey, handler, nil, nil)
	fmt.Println("ready")
	if err := srv.ListenAndServe(fmt.Sprintf("127.0.0.1:%d", *port)); err != nil {
		logger.Fatal(err)
	}
}
