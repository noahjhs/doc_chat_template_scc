//go:build windows

package secretstore

import (
	"bytes"
	"testing"
)

func TestDPAPIRoundTrip(t *testing.T) {
	s := Store{Root: t.TempDir(), Sealer: dpapi{}}
	if err := s.Set("Casper Test", "acct", []byte("hunter2")); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get("Casper Test", "acct")
	if err != nil || !bytes.Equal(got, []byte("hunter2")) {
		t.Fatalf("got %q %v", got, err)
	}
	raw, _ := Store{Root: s.Root, Sealer: nopSealer{}}.Get("Casper Test", "acct")
	if bytes.Contains(raw, []byte("hunter2")) {
		t.Fatal("stored in the clear")
	}
	if err := s.Set("Casper Test", "empty", nil); err != nil {
		t.Fatal(err)
	}
	if v, err := s.Get("Casper Test", "empty"); err != nil || len(v) != 0 {
		t.Fatalf("empty: %q %v", v, err)
	}
}

type nopSealer struct{}

func (nopSealer) Seal(p []byte) ([]byte, error)   { return p, nil }
func (nopSealer) Unseal(p []byte) ([]byte, error) { return p, nil }
