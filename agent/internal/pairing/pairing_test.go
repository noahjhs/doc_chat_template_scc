package pairing

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"
)

// captureNonceFromStdout reads the "Sign in to continue: <url>" line
// OpenPairingPageAndWait prints (its generated nonce is otherwise internal,
// not returned to callers) by temporarily redirecting os.Stdout, and
// extracts the nonce query param from that URL.
func captureNonceFromStdout(t *testing.T, start func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	origStdout := os.Stdout
	os.Stdout = w

	nonceCh := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(r)
		for scanner.Scan() {
			line := scanner.Text()
			if strings.Contains(line, "Sign in to continue:") {
				parts := strings.SplitN(line, "Sign in to continue: ", 2)
				if len(parts) == 2 {
					if u, err := url.Parse(strings.TrimSpace(parts[1])); err == nil {
						nonceCh <- u.Query().Get("nonce")
						return
					}
				}
			}
		}
	}()

	start() // non-blocking: kicks off OpenPairingPageAndWait in its own goroutine

	select {
	case n := <-nonceCh:
		os.Stdout = origStdout
		return n
	case <-time.After(2 * time.Second):
		os.Stdout = origStdout
		t.Fatal("never saw the printed sign-in URL")
		return ""
	}
}

// TestOpenPairingPageAndWait_SpinnerCanObserveReady reproduces a real
// regression: with a fast onAuthenticated (as it now is, since the
// relay-based tunnel returns instantly instead of the old cloudflared
// implementation's up-to-30s wait), the pairing listener could close
// (once authDone fires) before the spinner page's own polling JS ever got
// a chance to see chat_url via /status -- leaving it retrying forever
// against a port nothing answers anymore. This test plays the role of that
// polling JS directly against the real listener.
func TestOpenPairingPageAndWait_SpinnerCanObserveReady(t *testing.T) {
	const port = 18732 // fixed, unlikely-to-collide test port

	done := make(chan error, 1)
	nonce := captureNonceFromStdout(t, func() {
		go func() {
			err := OpenPairingPageAndWait("localhost:0", port, "", 5*time.Second, func(username, token string) string {
				// Mirrors the real onAuthenticated: near-instant now that
				// tunnel.Start doesn't block on network I/O.
				return "https://app.example.com/chat?ok=1"
			})
			done <- err
		}()
	})

	// Simulate the browser's callback request.
	resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/?nonce=%s&token=tok&username=alice", port, nonce))
	if err != nil {
		t.Fatalf("callback request failed: %v", err)
	}
	resp.Body.Close()

	// Simulate the spinner page's poll() loop: poll /status repeatedly,
	// exactly as the real JS does, and confirm it eventually reports ready
	// -- the regression manifested as this loop never seeing ready:true,
	// every request instead failing outright once the server closed early.
	deadline := time.Now().Add(3 * time.Second)
	sawReady := false
	for time.Now().Before(deadline) {
		statusResp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/status", port))
		if err != nil {
			time.Sleep(300 * time.Millisecond)
			continue
		}
		var body struct {
			Ready   bool   `json:"ready"`
			ChatURL string `json:"chat_url"`
		}
		decodeErr := json.NewDecoder(statusResp.Body).Decode(&body)
		statusResp.Body.Close()
		if decodeErr == nil && body.Ready && body.ChatURL != "" {
			sawReady = true
			break
		}
		time.Sleep(300 * time.Millisecond)
	}

	if !sawReady {
		t.Fatal("spinner polling never observed ready:true with a chat_url -- the listener likely closed before the poll loop could see it")
	}

	if err := <-done; err != nil {
		t.Fatalf("OpenPairingPageAndWait returned an error: %v", err)
	}
}
