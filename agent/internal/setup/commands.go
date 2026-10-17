package setup

import (
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"time"

	"casper-agent/internal/backup"
	"casper-agent/internal/config"
	"casper-agent/internal/mirrord"
	"casper-agent/internal/recovery"
	"casper-agent/internal/urlscheme"
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
		set("Open Casper: `" + openAppCommand(exe) + "`")
	} else {
		lines = append(lines, "Casper app: running")
	}

	acct, err := c.currentAccount()
	if names := loadIndex(); err != nil && len(names) > 1 && c.account == "" {
		// Several people's accounts on one Mac: never guess whose this is.
		lines = append(lines, "Accounts on this Mac: "+strings.Join(names, ", ")+" (all still set up)")
		set("Ask which account is the person's (or whether they need a new one), then add --account <name> to every setup command")
	} else if err != nil {
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
		kitFile, kitSaved := c.savedKitPath(acct.Username)
		st["recovery_kit"] = kitSaved
		if kitSaved {
			st["recovery_kit_path"] = kitFile
			lines = append(lines, "Recovery kit: saved at "+kitFile)
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

// openAppCommand is how to start the app from a shell.
func openAppCommand(exe string) string {
	if runtime.GOOS == "windows" {
		return `Start-Process "` + exe + `"`
	}
	return "open " + appBundle(exe)
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
	// Make sure the Keychain will take the password BEFORE signing up --
	// otherwise a refused save (e.g. the login keychain is unreachable from
	// an SSH session) leaves a real account nobody can ever sign in to.
	if err := keychainSave(c.authDomain+"/"+username, []byte(`{"pending":true}`)); err != nil {
		return fmt.Errorf("can't save to this Mac's Keychain (%v) -- if this is an SSH session, run setup in Terminal on the Mac itself, where the login keychain is available; nothing was created", err)
	}
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
	extra := ""
	if len(loadIndex()) > 1 {
		extra = fmt.Sprintf(" This Mac now has more than one Casper account, so add --account %s to the setup commands that follow.", username)
	}
	c.emit(fmt.Sprintf("Created Casper account %s. Its password is in this Mac's Keychain (\"Casper Account\"); nobody needs to type it.%s", username, extra),
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
		_ = urlscheme.Ensure() // Windows: the link must find the app before it has ever run
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
				c.emit("Connected. Restart required: tell the person to type /exit, run `claude`, and say \"continue Casper\" "+
					"(with their invite code, if they have one).",
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
		var profile struct {
			ChatID  string `json:"telegram_chat_id"`
			Enabled bool   `json:"telegram_notifications_enabled"`
		}
		if err := c.call("GET", "/profile", token, nil, &profile); err == nil && profile.ChatID != "" && profile.Enabled {
			c.emit("Telegram is already linked.", map[string]any{"ok": true, "available": true, "linked": true, "already": true})
			return nil
		}
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
		// Wait for them to tap Start, so the agent carries on by itself
		// instead of waiting to be told.
		for deadline := time.Now().Add(3 * time.Minute); time.Now().Before(deadline); time.Sleep(2 * time.Second) {
			if err := c.call("GET", "/profile", token, nil, &profile); err == nil && profile.ChatID != "" {
				c.emit("Linked: Telegram is connected. Carry on.", map[string]any{"ok": true, "available": true, "linked": true})
				return nil
			}
		}
		c.emit("Opened Telegram, but Start wasn't tapped yet. When it is, run this again to confirm. (Link: "+link.LinkURL+")",
			map[string]any{"ok": true, "available": true, "linked": false, "link_url": link.LinkURL})
		return nil
	})
}

// --- recovery kit ---------------------------------------------------------------------------

// kitAccount is whose kit this is, and how to tell Casper it was made. A Mac
// set up by an agent has a setup account; one paired by signing in in the
// browser (Casper's guide) has only the app's own pairing, which is enough:
// the kit needs just the username, and the Mac's device token can report it.
func (c *cli) kitAccount() (*storedAccount, func(), error) {
	if acct, err := c.currentAccount(); err == nil {
		return acct, func() {
			_ = c.authed(func(token string) error { return c.call("POST", "/profile/recovery-kit", token, nil, nil) })
		}, nil
	} else if c.account == "" {
		if sessions, _ := config.LoadSessions(); len(sessions) == 1 {
			s := sessions[0]
			return &storedAccount{Username: s.Username}, func() { _ = c.call("POST", "/profile/recovery-kit", s.DeviceToken, nil, nil) }, nil
		}
		return nil, nil, err
	} else {
		return nil, nil, err
	}
}

// recoveryKit is what a kit holds. Version 3 (recovery-v2-design.md 2.7):
// the master key, from which every newer folder's password derives, plus the
// frozen passwords of folders that predate it (LegacyBundle) and the backup
// keys. Version 2 (still read): every folder's password, so it goes stale
// when a folder is added. Locked with a passphrase the person types into a
// macOS dialog.
type recoveryKit struct {
	Version         int               `json:"version"`
	Account         string            `json:"account"`
	AuthDomain      string            `json:"auth_domain"`
	BackupKeys      []byte            `json:"backup_keys,omitempty"`
	MasterKey       []byte            `json:"master_key,omitempty"`              // v3
	LegacyBundle    map[string]string `json:"legacy_bundle_plaintext,omitempty"` // v3: folder ID -> password
	MirrorPasswords map[string]string `json:"mirror_passwords,omitempty"`        // v2: folder ID -> password
}

// folderPasswords is every folder password the kit carries outright.
func (k recoveryKit) folderPasswords() map[string]string {
	out := map[string]string{}
	for id, pw := range k.MirrorPasswords {
		out[id] = pw
	}
	for id, pw := range k.LegacyBundle {
		out[id] = pw
	}
	return out
}

func (c *cli) keys() *recovery.Keys {
	return NewKeys(recovery.KeychainStore{}, mirrord.KeychainPasswords{}, c.authDomain)
}

// NewKeys is the master-key source over a store and the legacy passwords.
func NewKeys(store recovery.KeyStore, pw mirrord.Passwords, authDomain string) *recovery.Keys {
	return &recovery.Keys{Store: store, Legacy: func(identity, folderID string) (string, error) {
		return pw.Get(mirrord.Account(authDomain, strings.TrimPrefix(identity, authDomain+"/"), folderID))
	}}
}

func (c *cli) recoveryKit() error {
	acct, report, err := c.kitAccount()
	if err != nil {
		return err
	}
	kit := recoveryKit{Version: 2, Account: acct.Username, AuthDomain: c.authDomain, MirrorPasswords: map[string]string{}}
	if m, _ := c.keys().Master(recovery.Identity(c.authDomain, acct.Username), false); m != nil {
		kit.Version, kit.MasterKey = 3, m
	}
	if data, _ := (backup.MacKeychain{}).Load(c.backupKeyAccount(acct.Username)); data != nil {
		kit.BackupKeys = data
	}
	prefix := mirrord.Account(c.authDomain, acct.Username, "")
	accounts, _ := mirrord.ListAccounts()
	for _, a := range accounts {
		if strings.HasPrefix(a, prefix) {
			if pw, err := (mirrord.KeychainPasswords{}).Get(a); err == nil && pw != "" {
				kit.MirrorPasswords[strings.TrimPrefix(a, prefix)] = pw
			}
		}
	}
	if kit.Version == 3 { // the stored passwords are the legacy ones; newer folders derive from the key
		kit.LegacyBundle, kit.MirrorPasswords = kit.MirrorPasswords, nil
	}
	if kit.BackupKeys == nil && len(kit.folderPasswords()) == 0 && kit.MasterKey == nil {
		return errors.New("this Mac has no keys or mirrored folders yet -- make the kit after setting up mirroring (or after pairing, for backups)")
	}
	// Decide where it goes before asking for a passphrase: never inside a
	// mirrored folder.
	roots, err := c.mirroredRoots(acct.Username)
	if err != nil {
		return err
	}
	moved, err := c.offerMoveOldKit(acct.Username, roots, "")
	if err != nil {
		return err
	}
	if moved != "" {
		c.emit(fmt.Sprintf("Moved the existing recovery kit out of a mirrored folder, to %s.", moved), map[string]any{"ok": true, "moved_to": moved})
		return nil
	}
	path, err := c.chooseKitPath(acct.Username, c.kitTo)
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
	text, err := kitFileText(kit, hostname(), time.Now(), pass)
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		return err
	}
	c.recordKitPath(acct.Username, path)
	// Tell Casper a kit was made (only that -- the kit stays here), so the
	// guide knows the step is done. Best effort.
	report()
	c.emit(fmt.Sprintf("Saved the recovery kit to %s. Tell the person to keep a copy of it, and the passphrase they chose, somewhere other than this Mac.", path),
		map[string]any{"ok": true, "path": path, "version": kit.Version, "mirrored_folders": len(kit.folderPasswords())})
	return nil
}

// restore imports a recovery kit on a (replacement) Mac: the folder
// passwords go into this Mac's Keychain, so it can rebuild mirrored folders
// from the mirrors (the agent then calls restore_folder).
func (c *cli) restore(kitPath string) error {
	if kitPath == "" {
		return errors.New("--kit <path to the recovery kit file> is required")
	}
	acct, err := c.currentAccount()
	if err != nil {
		return err
	}
	pass, err := askHidden("Passphrase for your Casper recovery kit:")
	if err != nil {
		return err
	}
	kit, err := ReadKitFile(kitPath, pass)
	if err != nil {
		return err
	}
	if kit.Account != "" && kit.Account != acct.Username {
		return fmt.Errorf("this kit is for %s, but this Mac is signed in as %s", kit.Account, acct.Username)
	}
	if err := importKitKeys(c.keys(), mirrord.KeychainPasswords{}, c.authDomain, acct.Username, kit); err != nil {
		return err
	}
	if kit.BackupKeys != nil {
		if existing, _ := (backup.MacKeychain{}).Load(c.backupKeyAccount(acct.Username)); existing == nil {
			_, _ = (backup.MacKeychain{}).Save(c.backupKeyAccount(acct.Username), kit.BackupKeys)
		}
	}
	c.emit("Imported the recovery kit: this Mac can now rebuild the mirrored folders (ask the agent to restore_folder, or choose Recover from a Lost Mac in Casper's menu).",
		map[string]any{"ok": true, "mirrored_folders": len(kit.folderPasswords())})
	return nil
}
