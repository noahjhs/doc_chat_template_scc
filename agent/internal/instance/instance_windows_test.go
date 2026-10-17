//go:build windows

package instance

import (
	"fmt"
	"os"
	"testing"
	"time"
)

func TestSecondClaimIsRefusedAndMessagesArrive(t *testing.T) {
	name := fmt.Sprintf("casper-test-%d", os.Getpid())
	got := make(chan string, 4)
	first, err := Acquire(name, func(s string) { got <- s })
	if err != nil || !first {
		t.Fatalf("first Acquire = %v, %v", first, err)
	}
	second, err := Acquire(name, func(string) {})
	if err != nil || second {
		t.Fatalf("second Acquire = %v, %v; want false", second, err)
	}
	for _, msg := range []string{"casper-test://pair?token=a&username=b", "show"} {
		if err := Send(name, msg); err != nil {
			t.Fatalf("Send(%q): %v", msg, err)
		}
		select {
		case s := <-got:
			if s != msg {
				t.Fatalf("got %q, want %q", s, msg)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("%q never arrived", msg)
		}
	}
	Release()
	if again, _ := Acquire(name, func(string) {}); !again {
		t.Fatal("after Release the claim should be free")
	}
	Release()
}

func TestSendWithNoHolderFails(t *testing.T) {
	if err := Send("casper-test-nobody", "x"); err == nil {
		t.Fatal("Send with no holder should fail")
	}
}
