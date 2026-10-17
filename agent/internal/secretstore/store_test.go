package secretstore

import (
	"bytes"
	"reflect"
	"sort"
	"testing"
)

// xorSealer stands in for DPAPI so the file layout is tested on any OS.
type xorSealer struct{}

func (xorSealer) Seal(p []byte) ([]byte, error)   { return xor(p), nil }
func (xorSealer) Unseal(p []byte) ([]byte, error) { return xor(p), nil }
func xor(p []byte) []byte {
	o := make([]byte, len(p))
	for i, b := range p {
		o[i] = b ^ 0x5a
	}
	return o
}

func TestStoreRoundTrip(t *testing.T) {
	s := Store{Root: t.TempDir(), Sealer: xorSealer{}}
	if v, err := s.Get("Svc", "a/b"); v != nil || err != nil {
		t.Fatalf("missing item: %v %v", v, err)
	}
	if err := s.Set("Svc", "auth.example/sam", []byte("secret")); err != nil {
		t.Fatal(err)
	}
	if err := s.Set("Svc", "auth.example/sam", []byte("secret2")); err != nil {
		t.Fatal(err)
	}
	if err := s.Set("Svc", "other", nil); err != nil {
		t.Fatal(err)
	}
	if err := s.Set("Svc2", "x", []byte("y")); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get("Svc", "auth.example/sam")
	if err != nil || !bytes.Equal(got, []byte("secret2")) {
		t.Fatalf("got %q %v", got, err)
	}
	names, _ := s.List("Svc")
	sort.Strings(names)
	if !reflect.DeepEqual(names, []string{"auth.example/sam", "other"}) {
		t.Fatalf("list: %v", names)
	}
	if n, _ := s.List("Nope"); len(n) != 0 {
		t.Fatalf("list of nothing: %v", n)
	}
}
