// Package registry tracks each connected agent's live WebSocket connection,
// keyed by the random routing key it registered under (never the agent's
// real API key -- see the relay's design notes for why).
package registry

import (
	"context"
	"sync"

	"github.com/coder/websocket"

	"casper-relay/internal/wire"
)

// Conn is the interface httpproxy/Registry depend on rather than the
// concrete *AgentConn -- lets tests substitute a fake connection with no
// real WebSocket involved.
type Conn interface {
	Do(ctx context.Context, frame wire.Frame) (wire.Frame, error)
	Close()
}

type Registry struct {
	mu    sync.Mutex
	conns map[string]Conn
}

func New() *Registry {
	return &Registry{conns: make(map[string]Conn)}
}

// NewConn wraps a freshly-accepted WebSocket connection for the given key.
func (r *Registry) NewConn(key string, conn *websocket.Conn) *AgentConn {
	return newAgentConn(key, conn)
}

// Register replaces any existing connection for key -- an agent reconnecting
// after a network blip should always win over a stale (possibly already
// dead) connection still sitting in the registry, rather than being
// rejected.
func (r *Registry) Register(key string, conn Conn) {
	r.mu.Lock()
	old := r.conns[key]
	r.conns[key] = conn
	r.mu.Unlock()
	if old != nil {
		old.Close()
	}
}

// Unregister removes conn from the registry, but only if it's still the
// current connection for key -- avoids a stale close racing with (and
// incorrectly evicting) a newer reconnect.
func (r *Registry) Unregister(key string, conn Conn) {
	r.mu.Lock()
	if r.conns[key] == conn {
		delete(r.conns, key)
	}
	r.mu.Unlock()
}

func (r *Registry) Get(key string) (Conn, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	c, ok := r.conns[key]
	return c, ok
}
