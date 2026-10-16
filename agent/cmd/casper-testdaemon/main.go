// Command casper-testdaemon is a headless stand-in for the real Casper.app,
// for integration tests only (tests/test_backup_integration.py) -- never
// built into a release. It runs the REAL daemon HTTP server, command
// handler and backup store for one already-paired identity, so a test can
// drive real encryption, signing, grant enforcement and restore through a
// real casper_service -- everything except the menu bar, the Keychain (an
// in-memory key store instead) and, by default, the relay (casper_service
// reaches it directly on localhost). With -relay-domain/-routing-key it
// joins the relay and reports presence every minute like the app does: a
// headless persona for end-to-end runs (tools/scenario.py), a Mac that's
// always on without a screen.
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
	"casper-agent/internal/recovery"
	"casper-agent/internal/server"
	"casper-agent/internal/setup"
	"casper-agent/internal/tunnel"
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
	relayDomain := flag.String("relay-domain", "", "join this relay and report presence (a headless persona)")
	routingKey := flag.String("routing-key", "", "the routing key paired with (with -relay-domain)")
	kitOut := flag.String("kit-out", "", "write a recovery kit (the folder passwords this daemon holds) to this file, then carry on")
	kitPass := flag.String("kit-pass", "", "the kit's passphrase (with -kit-out / -recover-kit)")
	keystore := flag.String("keystore", "", "file holding the master key (default: <data>/keystore.json)")
	kitNoBundle := flag.Bool("kit-no-bundle", false, "with -kit-out: leave the legacy folders' passwords out of the kit (the sealed copy on casper_service covers them)")
	kitOnly := flag.Bool("kit-only", false, "with -kit-out: write the kit and exit")
	recoverKit := flag.String("recover-kit", "", "recover from this kit file: import it, restore every folder it covers (the person's yes is given)")
	flag.Parse()

	passwords := &mirrord.FilePasswords{Path: filepath.Join(*data, "mirror-passwords.json")}
	if *keystore == "" {
		*keystore = filepath.Join(*data, "keystore.json")
	}
	masterKeys := setup.NewKeys(&recovery.FileStore{Path: *keystore}, passwords, *authDomain)
	if *kitOut != "" {
		legacy := passwords.All(mirrord.Account(*authDomain, *username, ""))
		kit := setup.NewKit(*authDomain, *username, nil, legacy)
		if m, _ := masterKeys.Master(recovery.Identity(*authDomain, *username), false); m != nil {
			if *kitNoBundle {
				legacy = nil
			}
			kit = setup.NewKitV3(*authDomain, *username, m, nil, legacy)
		}
		if err := setup.WriteKitFile(*kitOut, kit, *kitPass); err != nil {
			log.Fatal(err)
		}
		if *kitOnly {
			return
		}
	}

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
			Passwords:  passwords,
			Keys:       masterKeys,
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

	if *recoverKit != "" {
		kit, err := setup.ReadKitFile(*recoverKit, *kitPass)
		if err != nil {
			logger.Fatal(err)
		}
		where, err := setup.Recover(setup.RecoverArgs{
			AuthDomain: *authDomain, Username: *username, DeviceToken: *deviceToken, Home: *home, Kit: kit, Passwords: passwords, Keys: masterKeys,
			Confirm: func(string) bool { return true },
		})
		if err != nil {
			logger.Fatal(err)
		}
		logger.Printf("recovering %v", where)
	}

	srv := server.New(logger)
	srv.AddIdentity(*commandKey, handler, nil, nil)
	if *relayDomain != "" {
		tun, err := tunnel.Start(*relayDomain, *routingKey, *port)
		if err != nil {
			logger.Fatal(err)
		}
		go func() {
			for ; ; time.Sleep(time.Minute) {
				if config.ReportPresence(*authDomain, *deviceToken, tun.URL, *home) {
					logger.Fatal("the pairing is no longer recognized")
				}
			}
		}()
	}
	fmt.Println("ready")
	if err := srv.ListenAndServe(fmt.Sprintf("127.0.0.1:%d", *port)); err != nil {
		logger.Fatal(err)
	}
}
