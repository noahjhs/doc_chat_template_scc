// casper-relaycheck tests a Syncthing relay end to end, the way two Macs use
// it: one joins and waits; the other asks the relay to connect them; both
// join the session and pass bytes. It says which step fails.
//
//	cd tools/relaycheck && go run . 'relay://host:port/?id=RELAY-ID'
package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net/url"
	"os"
	"time"

	syncthingprotocol "github.com/syncthing/syncthing/lib/protocol"
	"github.com/syncthing/syncthing/lib/relay/client"
	"github.com/syncthing/syncthing/lib/tlsutil"
)

func step(name string, err error) {
	if err != nil {
		fmt.Printf("FAIL  %s: %v\n", name, err)
		os.Exit(1)
	}
	fmt.Printf("ok    %s\n", name)
}

func main() {
	uri, err := url.Parse(os.Args[1])
	step("parse relay address", err)
	certA, err := tlsutil.NewCertificateInMemory("syncthing", 1)
	step("make device A", err)
	certB, err := tlsutil.NewCertificateInMemory("syncthing", 1)
	step("make device B", err)
	idA := syncthingprotocol.NewDeviceID(certA.Certificate[0])

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	a, err := client.NewClient(uri, []tls.Certificate{certA}, 10*time.Second)
	step("A: relay client", err)
	go a.Serve(ctx)
	for i := 0; i < 50 && a.Error() != nil; i++ {
		time.Sleep(200 * time.Millisecond)
	}
	time.Sleep(2 * time.Second)
	if err := a.Error(); err != nil {
		step("A: joined the relay", err)
	}
	step("A: joined the relay", nil)

	inv, err := client.GetInvitationFromRelay(ctx, uri, idA, []tls.Certificate{certB}, 10*time.Second)
	step("B: asked the relay for a session with A", err)
	fmt.Printf("      session address %v:%d\n", inv.Address, inv.Port)

	invA := <-a.Invitations()
	step("A: got the relay's invitation", nil)
	done := make(chan error, 1)
	go func() {
		conn, err := client.JoinSession(ctx, invA)
		if err == nil {
			_, err = conn.Write([]byte("hello from A"))
		}
		done <- err
	}()
	connB, err := client.JoinSession(ctx, inv)
	step("B: joined the session", err)
	step("A: joined the session", <-done)
	buf := make([]byte, 12)
	connB.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, err = io.ReadFull(connB, buf)
	step(fmt.Sprintf("bytes passed A to B (%q)", buf), err)
}
