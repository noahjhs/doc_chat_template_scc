package wsconnect

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"casper-relay/internal/registry"
	"casper-relay/internal/wire"
)

// A fake agent answers one request with a 5MB body -- well past
// coder/websocket's 32KB default read limit, which the relay's end of the
// connection must have raised (see wire.MaxFrameBytes) for this to arrive.
func TestHandler_AcceptsLargeAgentResponses(t *testing.T) {
	reg := registry.New()
	server := httptest.NewServer(Handler(reg, log.New(io.Discard, "", 0)))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	agentConn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http")+"?key=k1", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer agentConn.CloseNow()

	big := strings.Repeat("x", 5<<20)
	go func() {
		_, data, err := agentConn.Read(ctx)
		if err != nil {
			return
		}
		var req wire.Frame
		_ = json.Unmarshal(data, &req)
		resp, _ := json.Marshal(wire.Frame{ID: req.ID, Status: 200, Body: base64.StdEncoding.EncodeToString([]byte(big))})
		_ = agentConn.Write(ctx, websocket.MessageText, resp)
	}()

	var conn registry.Conn
	for i := 0; i < 100; i++ {
		if c, ok := reg.Get("k1"); ok {
			conn = c
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if conn == nil {
		t.Fatal("agent never registered")
	}
	resp, err := conn.Do(ctx, wire.Frame{Method: "POST", Path: "/api/command"})
	if err != nil {
		t.Fatalf("large response didn't make it through: %v", err)
	}
	body, _ := base64.StdEncoding.DecodeString(resp.Body)
	if len(body) != len(big) {
		t.Fatalf("expected %d bytes, got %d", len(big), len(body))
	}
}
