// casper-mixedtest is a test-only tool for the mixed-pair check of
// docs/product/windows.md (phase 4): a Mac and a Windows PC mirror to each
// other through the real Manager and real Syncthing, with the Windows side
// run in the VM. Each side owns one folder (with awkward names and a path
// longer than 260 characters) and holds the other's, encrypted.
//
//	casper-mixedtest -role mac -syncthing BIN -dir D -listen tcp://0.0.0.0:22101 id      # prints "DEVICE <id>"
//	casper-mixedtest -role win ... -peer-id ID -peer-addr tcp://10.0.2.2:22101 run
//
// `id` creates the Syncthing identity and exits; `run` mirrors both ways and
// exits 0 once each side's folder is complete on the other and the other's is
// held here, printing a JSON line per check.
package main

import (
	"crypto/rand"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"casper-agent/internal/mirror"
)

func main() {
	role := flag.String("role", "", "mac or win")
	bin := flag.String("syncthing", "", "syncthing binary")
	dir := flag.String("dir", "", "work directory")
	listen := flag.String("listen", "", `syncthing listen address ("" = Casper's default for this OS)`)
	peerID := flag.String("peer-id", "", "the other side's device ID")
	peerAddr := flag.String("peer-addr", "", "an address to dial the other side on (optional)")
	timeout := flag.Duration("timeout", 3*time.Minute, "how long to wait")
	flag.Parse()
	if flag.NArg() != 1 || *role == "" || *bin == "" || *dir == "" {
		fmt.Fprintln(os.Stderr, "usage: casper-mixedtest -role mac|win -syncthing BIN -dir D [-listen A] [-peer-id ID -peer-addr A] id|run")
		os.Exit(2)
	}
	logf := func(f string, a ...any) { fmt.Fprintf(os.Stderr, f+"\n", a...) }
	m := mirror.New(*bin, filepath.Join(*dir, "st"), filepath.Join(*dir, "data"), *listen, logf)
	if err := m.Start(); err != nil {
		fail("start: %v", err)
	}
	defer m.Stop()
	// Stopped from outside (a timeout, a failed run): take Syncthing down too.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() { <-sig; m.Stop(); os.Exit(1) }()
	if flag.Arg(0) == "id" {
		fmt.Println("DEVICE", m.DeviceID())
		return
	}
	if *peerID == "" {
		fail("run needs -peer-id")
	}
	other := map[string]string{"mac": "win", "win": "mac"}[*role]
	own, theirs := "casper-mixed-"+*role, "casper-mixed-"+other
	src := filepath.Join(*dir, "owned")
	makeFiles(src, *role)

	var addrs []string
	if *peerAddr != "" {
		addrs = []string{*peerAddr}
	}
	peer := mirror.Peer{DeviceID: *peerID, Name: other, Addresses: addrs}
	f := mirror.OwnedFolder{ID: own, Label: *role + " files", Path: src, Mirrors: []string{*peerID}, Password: "a long folder password for the mixed test"}
	if err := m.Apply(mirror.Desired{
		Peers: []mirror.Peer{peer},
		Owned: []mirror.OwnedFolder{f},
		Held:  []mirror.HeldFolder{{ID: theirs, Label: other, Owners: []string{*peerID}, Kind: "mirror"}},
	}); err != nil {
		fail("apply: %v", err)
	}

	deadline := time.Now().Add(*timeout)
	var ownDone, heldDone bool
	for time.Now().Before(deadline) {
		st, _ := m.OwnedStatus(f)
		ownDone = st.Error == "" && st.UnreadableItems == 0 && st.AtRiskFiles == 0 && len(st.Peers) == 1 && st.Peers[0].Completion == 100
		held := m.HeldPath(theirs)
		heldDone = mirror.DirSize(held) >= 3<<20 && !leaks(held)
		report(map[string]any{"role": *role, "own_complete": ownDone, "held_bytes": mirror.DirSize(m.HeldPath(theirs)), "peer_connected": len(st.Peers) == 1 && st.Peers[0].Connected, "files": st.LocalFiles})
		if ownDone && heldDone {
			// Stay up a moment so the other side sees this one finish too.
			time.Sleep(10 * time.Second)
			report(map[string]any{"role": *role, "result": "ok"})
			return
		}
		time.Sleep(2 * time.Second)
	}
	report(map[string]any{"role": *role, "result": "timeout", "own_complete": ownDone, "held_ok": heldDone})
	os.Exit(1)
}

// makeFiles lays out what makes Windows interesting: a path past 260
// characters, non-ASCII names, spaces, a name that differs only in case from
// another folder's, and a few MB of random bytes.
func makeFiles(root, role string) {
	long := root
	for i := 0; i < 12; i++ {
		long = filepath.Join(long, strings.Repeat("d", 24)+fmt.Sprint(i))
	}
	must(os.MkdirAll(long, 0o755))
	must(os.WriteFile(filepath.Join(long, "deep file.txt"), []byte("below 260+ characters from "+role), 0o644))
	must(os.WriteFile(filepath.Join(root, "Café – 日本語 notes.txt"), []byte("unicode from "+role), 0o644))
	must(os.WriteFile(filepath.Join(root, "Report.txt"), []byte("capital R, from "+role), 0o644))
	must(os.MkdirAll(filepath.Join(root, "My Folder", "sub folder"), 0o755))
	big := make([]byte, 3<<20)
	_, _ = rand.Read(big)
	must(os.WriteFile(filepath.Join(root, "My Folder", "sub folder", "big.bin"), big, 0o644))
}

// leaks: the held copy must be encrypted -- none of the plain names appear.
func leaks(held string) bool {
	found := false
	_ = filepath.Walk(held, func(p string, info os.FileInfo, err error) error {
		if err == nil {
			for _, n := range []string{"deep file", "Café", "Report", "My Folder", "big.bin"} {
				if strings.Contains(info.Name(), n) {
					found = true
				}
			}
		}
		return nil
	})
	return found
}

func report(v map[string]any) { b, _ := json.Marshal(v); fmt.Println(string(b)) }

func must(err error) {
	if err != nil {
		fail("%v", err)
	}
}

func fail(f string, a ...any) {
	fmt.Fprintf(os.Stderr, "casper-mixedtest: "+f+"\n", a...)
	os.Exit(1)
}
