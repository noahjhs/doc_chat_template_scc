package setup

import (
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"casper-agent/internal/backup"
	"casper-agent/internal/config"
)

// --- status ---------------------------------------------------------------------

type hostView struct {
	HostID     int    `json:"host_id"`
	Label      string `json:"label"`
	Hostname   string `json:"hostname"`
	Connected  bool   `json:"connected"`
	CommandKey string `json:"command_key"`
}

func (c *cli) thisMac(token string) (*hostView, error) {
	var resp struct {
		Hosts []hostView `json:"hosts"`
	}
	if err := c.call("GET", "/hosts", token, nil, &resp); err != nil {
		return nil, err
	}
	for _, h := range resp.Hosts {
		if h.Hostname == hostname() && h.CommandKey != "" {
			return &h, nil
		}
	}
	return nil, nil
}

func appRunning() bool {
	port := os.Getenv("CONTROL_TOOL_PORT")
	if port == "" {
		port = "8000"
	}
	resp, err := (&http.Client{Timeout: 2 * time.Second}).Get("http://127.0.0.1:" + port + "/api/health")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return true
}

func claudeHasCasper() bool {
	if _, err := exec.LookPath("claude"); err != nil {
		return false
	}
	return exec.Command("claude", "mcp", "get", "casper").Run() == nil
}

func (c *cli) status() error {
	exe, _ := os.Executable()
	st := map[string]any{"ok": true, "casper_service": c.authDomain, "app": exe, "app_running": appRunning(), "accounts": loadIndex()}
	var lines []string
	next := ""
	set := func(step string) {
		if next == "" {
			next = step
		}
	}
	if !st["app_running"].(bool) {
		lines = append(lines, "Casper app: not running")
		set("Open Casper: `open " + appBundle(exe) + "`")
	} else {
		lines = append(lines, "Casper app: running")
	}

	acct, err := c.currentAccount()
	if err != nil {
		lines = append(lines, "Account: none on this Mac")
		set("Create an account: `setup account create --username <name>` (ask the person what name their friends should know them by)")
	} else {
		st["account"] = acct.Username
		lines = append(lines, "Account: "+acct.Username)
		var mac *hostView
		err := c.authed(func(token string) error {
			var err error
			mac, err = c.thisMac(token)
			if err != nil {
				return err
			}
			var tokens struct {
				AgentTokens []struct {
					Name    string `json:"name"`
					Revoked bool   `json:"revoked"`
				} `json:"agent_tokens"`
			}
			if err := c.call("GET", "/agent-tokens", token, nil, &tokens); err != nil {
				return err
			}
			agent := false
			for _, t := range tokens.AgentTokens {
				if !t.Revoked && strings.HasPrefix(t.Name, "setup:") {
					agent = true
				}
			}
			st["agent_token"] = agent
			var profile struct {
				ChatID  string `json:"telegram_chat_id"`
				Enabled bool   `json:"telegram_notifications_enabled"`
			}
			if err := c.call("GET", "/profile", token, nil, &profile); err != nil {
				return err
			}
			st["notifications"] = profile.ChatID != "" && profile.Enabled
			return nil
		})
		if err != nil {
			return err
		}
		switch {
		case mac == nil:
			lines = append(lines, "This Mac: not paired")
			set("Pair this Mac: `setup pair`")
		case !mac.Connected:
			st["this_mac"] = mac.Label
			lines = append(lines, "This Mac: paired as "+mac.Label+", but not connected")
			set("Make sure Casper is running (and its menu-bar ghost isn't paused), then run `setup status` again")
		default:
			st["this_mac"] = mac.Label
			lines = append(lines, "This Mac: paired and connected as "+mac.Label)
		}
		claude := claudeHasCasper()
		st["claude_code_connected"] = claude
		if st["agent_token"].(bool) {
			lines = append(lines, fmt.Sprintf("Agent: connected (Claude Code configured: %v)", claude))
		} else {
			lines = append(lines, "Agent: not connected")
			set("Connect the agent: `setup agent`, then restart the agent")
		}
		if st["notifications"].(bool) {
			lines = append(lines, "Notifications: Telegram linked")
		} else {
			lines = append(lines, "Notifications: not linked (optional -- `setup notifications`)")
		}
		_, kitErr := os.Stat(recoveryKitPath(acct.Username))
		st["recovery_kit"] = kitErr == nil
		if kitErr == nil {
			lines = append(lines, "Recovery kit: saved in Documents")
		} else {
			lines = append(lines, "Recovery kit: not made (needed before relying on backups -- `setup recovery-kit`)")
		}
	}
	if next == "" {
		next = "Setup is done. Use Casper's MCP tools (start with my_casper) to share space or back up."
	}
	st["next_step"] = next
	c.emit(strings.Join(lines, "\n")+"\nNext: "+next, st)
	return nil
}

func appBundle(exe string) string {
	if i := strings.Index(exe, ".app/"); i >= 0 {
		return exe[:i+4]
	}
	return exe
}

// --- account ----------------------------------------------------------------------

func (c *cli) accountCreate(username string) error {
	if username == "" {
		return errors.New("--username is required")
	}
	if existing, _ := loadAccount(c.authDomain, username); existing != nil {
		c.account = username
		return c.authed(func(token string) error {
			c.emit("Account "+username+" is already set up on this Mac.", map[string]any{"ok": true, "username": username, "created": false})
			return nil
		})
	}
	password := randomPassword()
	var resp struct {
		Token string `json:"token"`
	}
	if err := c.call("POST", "/signup", "", map[string]string{"username": username, "password": password}, &resp); err != nil {
		var ae *apiError
		if errors.As(err, &ae) && ae.status == 409 {
			return fmt.Errorf("the name %q is taken -- ask the person for another (or, if it's theirs from another Mac, use `setup account login`)", username)
		}
		return err
	}
	if err := saveAccount(c.authDomain, &storedAccount{Username: username, Password: password, Token: resp.Token}); err != nil {
		return err
	}
	c.emit(fmt.Sprintf("Created Casper account %s. Its password is in this Mac's Keychain (\"Casper Account\"); nobody needs to type it.", username),
		map[string]any{"ok": true, "username": username, "created": true})
	return nil
}

func (c *cli) accountLogin(username string) error {
	if username == "" {
		return errors.New("--username is required")
	}
	password, err := askHidden("Casper password for " + username + ":")
	if err != nil {
		return err
	}
	acct := &storedAccount{Username: username, Password: password}
	if err := c.relogin(acct); err != nil {
		return err
	}
	c.emit("Signed in as "+username+".", map[string]any{"ok": true, "username": username})
	return nil
}

// --- pair -----------------------------------------------------------------------------

func (c *cli) pair() error {
	acct, err := c.currentAccount()
	if err != nil {
		return err
	}
	return c.authed(func(token string) error {
		if mac, err := c.thisMac(token); err != nil {
			return err
		} else if mac != nil && mac.Connected {
			c.emit("This Mac is already paired and connected as "+mac.Label+".", map[string]any{"ok": true, "host": mac.Label, "already": true})
			return nil
		}
		started := time.Now()
		if err := openURL(pairURL(token, acct.Username)); err != nil {
			return fmt.Errorf("couldn't hand the pairing to Casper.app (is it installed?): %w", err)
		}
		deadline := time.Now().Add(60 * time.Second)
		for time.Now().Before(deadline) {
			time.Sleep(time.Second)
			if st, _ := config.LoadLastPairingResult(); st != nil && st.At.After(started) && st.Result != "ok" {
				return errors.New("pairing failed: " + st.Message)
			}
			if mac, err := c.thisMac(token); err == nil && mac != nil && mac.Connected {
				c.emit("Paired: this Mac is connected to Casper as "+mac.Label+".", map[string]any{"ok": true, "host": mac.Label})
				return nil
			}
		}
		return errors.New("timed out waiting for this Mac to connect -- check Casper is running (menu-bar ghost), and that any first-launch dialog was answered, then run `setup pair` again")
	})
}

// --- agent ------------------------------------------------------------------------------

func (c *cli) agent(client string) error {
	return c.authed(func(token string) error {
		name := fmt.Sprintf("setup: %s on %s", client, hostname())
		var list struct {
			AgentTokens []struct {
				ID      int    `json:"id"`
				Name    string `json:"name"`
				Revoked bool   `json:"revoked"`
			} `json:"agent_tokens"`
		}
		if err := c.call("GET", "/agent-tokens", token, nil, &list); err != nil {
			return err
		}
		for _, t := range list.AgentTokens {
			if t.Name == name && !t.Revoked {
				_ = c.call("DELETE", fmt.Sprintf("/agent-tokens/%d", t.ID), token, nil, nil)
			}
		}
		var created struct {
			Token string `json:"token"`
		}
		if err := c.call("POST", "/agent-tokens", token, map[string]string{"name": name}, &created); err != nil {
			return err
		}
		mcpURL := config.BaseURL(c.authDomain) + "/mcp"
		if client == "claude-code" {
			if _, err := exec.LookPath("claude"); err == nil {
				_ = exec.Command("claude", "mcp", "remove", "casper", "--scope", "user").Run()
				out, err := exec.Command("claude", "mcp", "add", "--transport", "http", "--scope", "user", "casper", mcpURL,
					"--header", "Authorization: Bearer "+created.Token).CombinedOutput()
				if err != nil {
					return fmt.Errorf("claude mcp add failed: %s", strings.TrimSpace(string(out)))
				}
				c.emit("Connected: Claude Code now has Casper as an MCP server (\"casper\").\n"+
					"Claude Code only loads new MCP servers when it starts, so the person needs to restart it once: "+
					"quit this session, start `claude` again in the same folder, and say \"continue\".",
					map[string]any{"ok": true, "client": client, "configured": true, "restart_required": true})
				return nil
			}
		}
		c.emit(fmt.Sprintf("Agent token created. Configure your agent with an HTTP MCP server:\n  url: %s\n  header: Authorization: Bearer %s\nThen restart the agent.", mcpURL, created.Token),
			map[string]any{"ok": true, "client": client, "configured": false, "mcp_url": mcpURL, "authorization_header": "Bearer " + created.Token})
		return nil
	})
}

// --- notifications ------------------------------------------------------------------------

func (c *cli) notifications() error {
	return c.authed(func(token string) error {
		var link struct {
			LinkURL string `json:"link_url"`
		}
		if err := c.call("POST", "/telegram/link", token, nil, &link); err != nil {
			var ae *apiError
			if errors.As(err, &ae) && ae.status == 503 {
				c.emit("Telegram isn't available on this Casper deployment -- skip this step.", map[string]any{"ok": true, "available": false})
				return nil
			}
			return err
		}
		if err := c.call("PATCH", "/profile", token, map[string]bool{"telegram_notifications_enabled": true}, nil); err != nil {
			return err
		}
		_ = openURL(link.LinkURL)
		c.emit("Opened Telegram. The person taps Start in the Casper bot's chat -- that's all. (Link, if it didn't open: "+link.LinkURL+")",
			map[string]any{"ok": true, "available": true, "link_url": link.LinkURL})
		return nil
	})
}

// --- recovery kit ---------------------------------------------------------------------------

func (c *cli) recoveryKit() error {
	acct, err := c.currentAccount()
	if err != nil {
		return err
	}
	data, err := backup.MacKeychain{}.Load(c.backupKeyAccount(acct.Username))
	if err != nil {
		return err
	}
	if data == nil {
		return errors.New("this Mac has no backup keys yet -- Casper creates them when the Mac is paired (`setup pair`), then try again")
	}
	keys, _, err := backup.LoadOrCreateKeys(backup.MacKeychain{}, c.backupKeyAccount(acct.Username))
	if err != nil {
		return err
	}
	pass, err := askHidden("Choose a passphrase for your Casper recovery kit (12+ characters). Keep it somewhere other than this Mac.")
	if err != nil {
		return err
	}
	again, err := askHidden("Type the passphrase again:")
	if err != nil {
		return err
	}
	if pass != again {
		return errors.New("the two passphrases didn't match -- run `setup recovery-kit` again")
	}
	armored, err := backup.ExportKeys(keys, pass)
	if err != nil {
		return err
	}
	kit := fmt.Sprintf(`Casper recovery kit -- %s (made on %s, %s)

This file holds the keys that decrypt the backups this Mac makes, locked with
the passphrase you chose. If this Mac is lost, these keys plus that passphrase
are the only way to restore those backups. Keep a copy of this file, and the
passphrase, somewhere other than this Mac (a password manager is ideal).

%s`, acct.Username, hostname(), time.Now().Format("2 Jan 2006"), armored)
	path := recoveryKitPath(acct.Username)
	if err := os.WriteFile(path, []byte(kit), 0o600); err != nil {
		return err
	}
	c.emit("Saved the recovery kit to "+path+". Tell the person to keep a copy of it, and the passphrase they chose, somewhere other than this Mac.",
		map[string]any{"ok": true, "path": path})
	return nil
}
