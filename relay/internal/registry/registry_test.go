package registry

import (
	"context"
	"testing"

	"casper-relay/internal/wire"
)

// fakeConn is a Conn with no real WebSocket involved -- lets tests drive
// exact response/error/timeout behavior deterministically.
type fakeConn struct {
	resp   wire.Frame
	err    error
	closed bool
	block  chan struct{} // if non-nil, Do blocks until this (or ctx) is done
}

func (f *fakeConn) Do(ctx context.Context, _ wire.Frame) (wire.Frame, error) {
	if f.block != nil {
		select {
		case <-f.block:
		case <-ctx.Done():
			return wire.Frame{}, ctx.Err()
		}
	}
	if f.err != nil {
		return wire.Frame{}, f.err
	}
	return f.resp, nil
}

func (f *fakeConn) Close() { f.closed = true }

func TestRegistry_RegisterGetUnregister(t *testing.T) {
	r := New()
	if _, ok := r.Get("k1"); ok {
		t.Fatal("expected no connection registered yet")
	}

	c1 := &fakeConn{}
	r.Register("k1", c1)
	got, ok := r.Get("k1")
	if !ok || got != c1 {
		t.Fatalf("expected to get back c1, got %v ok=%v", got, ok)
	}

	r.Unregister("k1", c1)
	if _, ok := r.Get("k1"); ok {
		t.Fatal("expected no connection after unregister")
	}
}

func TestRegistry_RegisterReplacesAndClosesOld(t *testing.T) {
	r := New()
	c1 := &fakeConn{}
	c2 := &fakeConn{}
	r.Register("k1", c1)
	r.Register("k1", c2) // simulates a reconnect

	if !c1.closed {
		t.Fatal("expected the replaced (stale) connection to be closed")
	}
	got, ok := r.Get("k1")
	if !ok || got != c2 {
		t.Fatalf("expected c2 to be the current connection, got %v ok=%v", got, ok)
	}
}

func TestRegistry_UnregisterOnlyRemovesIfStillCurrent(t *testing.T) {
	r := New()
	c1 := &fakeConn{}
	c2 := &fakeConn{}
	r.Register("k1", c1)
	r.Register("k1", c2) // c1 is now stale

	// A late Unregister("k1", c1) -- e.g. c1's own readerLoop finally
	// noticing its connection died, racing after c2 already took over --
	// must not evict c2.
	r.Unregister("k1", c1)
	got, ok := r.Get("k1")
	if !ok || got != c2 {
		t.Fatalf("stale unregister incorrectly evicted the current connection: got %v ok=%v", got, ok)
	}
}
