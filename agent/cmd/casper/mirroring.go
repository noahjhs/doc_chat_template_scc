package main

import (
	"os"
	"path/filepath"
	"time"

	"casper-agent/internal/config"
	"casper-agent/internal/dialog"
	"casper-agent/internal/mirror"
	"casper-agent/internal/mirrord"
)

// startMirroring runs the bundled Syncthing (Contents/MacOS/syncthing, next
// to this executable) as Casper's mirroring engine. Mirroring is simply off
// if the binary isn't there (e.g. a bare `go build` during development).
func (d *daemonState) startMirroring() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	bin := os.Getenv("CASPER_SYNCTHING")
	if bin == "" {
		bin = filepath.Join(filepath.Dir(exe), "syncthing")
	}
	if _, err := os.Stat(bin); err != nil {
		d.logf("mirroring: off (no bundled syncthing at %s)", bin)
		return
	}
	dir, err := config.AppConfigDir()
	if err != nil {
		return
	}
	svc := &mirrord.Service{
		AuthDomain: d.authDomain,
		Mgr:        mirror.New(bin, filepath.Join(dir, "syncthing"), filepath.Join(dir, "mirror"), "default", d.logf),
		Bin:        bin,
		Passwords:  mirrord.KeychainPasswords{},
		Identities: d.mirrorIdentities,
		Ask: func(text string) (bool, bool) {
			return dialog.Confirm(text, "Allow", "Don't Allow", 10*time.Minute)
		},
		Logf: d.logf,
	}
	d.mu.Lock()
	d.mirror = svc
	for username, pi := range d.identities {
		pi.handler.SetMirrorOps(svc.Ops(username))
	}
	d.mu.Unlock()
	go svc.Run()
}

func (d *daemonState) mirrorIdentities() []mirrord.Identity {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]mirrord.Identity, 0, len(d.identities))
	for username, pi := range d.identities {
		out = append(out, mirrord.Identity{Username: username, DeviceToken: pi.deviceToken})
	}
	return out
}

func (d *daemonState) stopMirroring() {
	d.mu.Lock()
	svc := d.mirror
	d.mu.Unlock()
	if svc != nil {
		svc.Mgr.Stop()
	}
}
