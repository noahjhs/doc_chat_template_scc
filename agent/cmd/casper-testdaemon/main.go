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
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"casper-agent/internal/backup"
	"casper-agent/internal/commands"
	"casper-agent/internal/config"
	"casper-agent/internal/mirror"
	"casper-agent/internal/mirrord"
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
	syncthing := flag.String("syncthing", "", "syncthing binary (enables mirroring)")
	stListen := flag.String("st-listen", "", "syncthing listen address, e.g. tcp://127.0.0.1:22101")
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

	if *syncthing != "" {
		svc := &mirrord.Service{
			AuthDomain: *authDomain,
			Mgr:        mirror.New(*syncthing, filepath.Join(*data, "syncthing"), filepath.Join(*data, "mirror"), *stListen, logger.Printf),
			Bin:        *syncthing,
			Passwords:  &mirrord.FilePasswords{Path: filepath.Join(*data, "mirror-passwords.json")},
			Identities: func() []mirrord.Identity { return []mirrord.Identity{{Username: *username, DeviceToken: *deviceToken}} },
			Logf:       logger.Printf,
			Interval:   2 * time.Second, // tests want quick reconciliation
		}
		handler.SetMirrorOps(svc.Ops(*username))
		go svc.Run()
		// Stop Syncthing with us, so a test "turning a Mac off" really does.
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGTERM, os.Interrupt)
		go func() {
			<-sig
			svc.Mgr.Stop()
			os.Exit(0)
		}()
	}

	srv := server.New(logger)
	srv.AddIdentity(*commandKey, handler, nil, nil)
	fmt.Println("ready")
	if err := srv.ListenAndServe(fmt.Sprintf("127.0.0.1:%d", *port)); err != nil {
		logger.Fatal(err)
	}
}
