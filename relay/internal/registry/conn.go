package registry

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"sync"
	"sync/atomic"

	"github.com/coder/websocket"

	"casper-relay/internal/wire"
)

var errClosed = errors.New("connection closed")

// AgentConn wraps one agent's live WebSocket connection. A single dedicated
// writer goroutine drains outbound (WS requires one writer at a time);
// each inbound Frame read is matched against a pending map by ID so
// multiple requests (e.g. concurrent tool calls) can be in flight at once.
type AgentConn struct {
	key    string
	conn   *websocket.Conn
	nextID uint64

	outbound chan wire.Frame

	mu      sync.Mutex
	pending map[string]chan wire.Frame

	closeOnce sync.Once
	closed    chan struct{}
}

func newAgentConn(key string, conn *websocket.Conn) *AgentConn {
	return &AgentConn{
		key:      key,
		conn:     conn,
		outbound: make(chan wire.Frame, 16),
		pending:  make(map[string]chan wire.Frame),
		closed:   make(chan struct{}),
	}
}

// Serve runs the connection's write and read loops, blocking until the
// connection closes (either side) or ctx is cancelled. Called once per
// connection by the /connect handler.
func (c *AgentConn) Serve(ctx context.Context) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		c.writerLoop(ctx)
	}()
	c.readerLoop(ctx)
	c.Close()
	<-done
}

func (c *AgentConn) writerLoop(ctx context.Context) {
	for {
		select {
		case frame, ok := <-c.outbound:
			if !ok {
				return
			}
			data, err := json.Marshal(frame)
			if err != nil {
				continue // malformed frame -- drop rather than wedge the writer
			}
			if err := c.conn.Write(ctx, websocket.MessageText, data); err != nil {
				return
			}
		case <-c.closed:
			return
		case <-ctx.Done():
			return
		}
	}
}

func (c *AgentConn) readerLoop(ctx context.Context) {
	for {
		_, data, err := c.conn.Read(ctx)
		if err != nil {
			return
		}
		var frame wire.Frame
		if err := json.Unmarshal(data, &frame); err != nil {
			continue // malformed frame from the agent -- ignore, not fatal to the connection
		}
		c.mu.Lock()
		ch, ok := c.pending[frame.ID]
		if ok {
			delete(c.pending, frame.ID)
		}
		c.mu.Unlock()
		if ok {
			ch <- frame
		}
		// else: a late response for a request the relay already gave up
		// on (timed out) -- correctly dropped, matching the design's
		// documented "log and drop" behavior for that case.
	}
}

// Do sends frame to the agent (assigning it a fresh correlation ID) and
// blocks for the matching response, or until ctx is done.
func (c *AgentConn) Do(ctx context.Context, frame wire.Frame) (wire.Frame, error) {
	id := strconv.FormatUint(atomic.AddUint64(&c.nextID, 1), 10)
	frame.ID = id

	respCh := make(chan wire.Frame, 1)
	c.mu.Lock()
	c.pending[id] = respCh
	c.mu.Unlock()
	cleanup := func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}

	select {
	case c.outbound <- frame:
	case <-ctx.Done():
		cleanup()
		return wire.Frame{}, ctx.Err()
	case <-c.closed:
		cleanup()
		return wire.Frame{}, errClosed
	}

	select {
	case resp := <-respCh:
		return resp, nil
	case <-ctx.Done():
		cleanup()
		return wire.Frame{}, ctx.Err()
	case <-c.closed:
		cleanup()
		return wire.Frame{}, errClosed
	}
}

func (c *AgentConn) Close() {
	c.closeOnce.Do(func() {
		close(c.closed)
		_ = c.conn.Close(websocket.StatusNormalClosure, "closing")
	})
}
