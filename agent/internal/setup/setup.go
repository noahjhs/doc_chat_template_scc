// Package setup is `Casper setup <command>`: the mechanics of onboarding,
// built into the Casper app binary so a person's agent can set Casper up for
// them (docs/product/scenarios/agent-onboarding.md). Principle: mechanics in
// tools, judgement in instructions -- every command here does one thing
// exactly right, is safe to re-run, and says what to do next. The
// conversation with the person lives in the onboarding instructions
// (onboarding/AGENTS.md and its skills), not here.
//
// Secrets never pass through the agent: the account password is generated
// and kept in the Keychain, and the recovery passphrase is typed into a macOS
// dialog the agent can't see.
package setup

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"casper-agent/internal/config"
)

// Main runs one setup command and returns the process exit code.
func Main(args []string) int {
	if len(args) == 0 || args[0] == "help" || args[0] == "--help" || args[0] == "-h" {
		fmt.Print(usage)
		return 0
	}
	authDomain, err := config.LoadAuthDomain()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	c := &cli{authDomain: authDomain, out: os.Stdout}
	cmd, rest := args[0], args[1:]
	if cmd == "account" && len(rest) > 0 {
		cmd, rest = "account "+rest[0], rest[1:]
	}
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	fs.BoolVar(&c.json, "json", false, "print JSON")
	fs.StringVar(&c.account, "account", "", "which Casper account (when this Mac has more than one)")
	username := fs.String("username", "", "username")
	client := fs.String("client", "claude-code", "which agent to connect")
	if err := fs.Parse(rest); err != nil {
		return 2
	}

	var run func() error
	switch cmd {
	case "status":
		run = c.status
	case "account create":
		run = func() error { return c.accountCreate(*username) }
	case "account login":
		run = func() error { return c.accountLogin(*username) }
	case "pair":
		run = c.pair
	case "agent":
		run = func() error { return c.agent(*client) }
	case "notifications":
		run = c.notifications
	case "recovery-kit":
		run = c.recoveryKit
	default:
		fmt.Fprintf(os.Stderr, "Unknown command %q.\n\n%s", cmd, usage)
		return 2
	}
	if err := run(); err != nil {
		c.fail(err)
		return 1
	}
	return 0
}

const usage = `Casper setup -- the mechanics of setting Casper up, for you or your agent.
Every command is safe to re-run.

  status                        what's done, and the next step
  account create --username U   create a Casper account (password kept in the Keychain)
  account login --username U    sign in to an existing account (asks in a dialog)
  pair                          connect this Mac to the account
  agent [--client claude-code]  connect your AI agent to Casper (then restart the agent)
  notifications                 link Telegram for nudges and approvals (optional)
  recovery-kit                  save this Mac's backup keys, protected by a passphrase you type

Flags: --json (machine-readable), --account U (if this Mac has several accounts).
`

type cli struct {
	authDomain string
	account    string
	json       bool
	out        io.Writer
}

// --- Output -------------------------------------------------------------------

func (c *cli) emit(text string, data map[string]any) {
	if c.json {
		_ = json.NewEncoder(c.out).Encode(data)
		return
	}
	fmt.Fprintln(c.out, text)
}

func (c *cli) fail(err error) {
	if c.json {
		_ = json.NewEncoder(c.out).Encode(map[string]any{"ok": false, "error": err.Error()})
		return
	}
	fmt.Fprintln(os.Stderr, "Error:", err)
}

// --- HTTP to casper_service -----------------------------------------------------

type apiError struct {
	status int
	detail string
}

func (e *apiError) Error() string { return e.detail }

func (c *cli) call(method, path, token string, body any, out any) error {
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, config.BaseURL(c.authDomain)+path, r)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return fmt.Errorf("can't reach Casper (%s): %w", c.authDomain, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var d struct {
			Detail any `json:"detail"`
		}
		_ = json.Unmarshal(data, &d)
		return &apiError{status: resp.StatusCode, detail: fmt.Sprint(d.Detail)}
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

// authed runs fn with a valid session token, signing in again with the
// stored password if the stored token has gone stale (any sign-in elsewhere
// rotates it).
func (c *cli) authed(fn func(token string) error) error {
	acct, err := c.currentAccount()
	if err != nil {
		return err
	}
	err = fn(acct.Token)
	var ae *apiError
	if errors.As(err, &ae) && ae.status == 401 {
		if err := c.relogin(acct); err != nil {
			return err
		}
		return fn(acct.Token)
	}
	return err
}

func (c *cli) relogin(acct *storedAccount) error {
	var resp struct {
		Token string `json:"token"`
	}
	if err := c.call("POST", "/login", "", map[string]string{"username": acct.Username, "password": acct.Password}, &resp); err != nil {
		return fmt.Errorf("signing in as %s: %w", acct.Username, err)
	}
	acct.Token = resp.Token
	return saveAccount(c.authDomain, acct)
}

// --- Helpers ----------------------------------------------------------------------

func randomPassword() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func hostname() string {
	h, _ := os.Hostname()
	return h
}

// askHidden shows a native macOS dialog with a hidden text field and
// returns what the person typed -- the agent driving this command never
// sees it.
func askHidden(prompt string) (string, error) {
	script := fmt.Sprintf(`set r to display dialog %q default answer "" with hidden answer with title "Casper" buttons {"Cancel", "OK"} default button "OK" giving up after 180`, prompt)
	out, err := exec.Command("osascript", "-e", script, "-e", `if gave up of r then error "timed out"`, "-e", "text returned of r").Output()
	if err != nil {
		return "", errors.New("the dialog was cancelled or not answered within 3 minutes -- run the command again when the person is ready")
	}
	return strings.TrimRight(string(out), "\n"), nil
}

func openURL(u string) error { return exec.Command("open", u).Run() }

func pairURL(token, username string) string {
	return fmt.Sprintf("%s://pair?token=%s&username=%s", config.PairURLScheme(), url.QueryEscape(token), url.QueryEscape(username))
}

func documentsDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Documents")
}

func recoveryKitPath(username string) string {
	return filepath.Join(documentsDir(), fmt.Sprintf("Casper Recovery Kit (%s).txt", username))
}

// backupKeyAccount mirrors cmd/casper/daemon.go's setUpBackups: the daemon
// files each identity's backup keys under "<auth domain>/<username>".
func (c *cli) backupKeyAccount(username string) string { return c.authDomain + "/" + username }

