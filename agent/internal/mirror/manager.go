// Package mirror runs Syncthing as Casper's mirroring engine
// (docs/product/scenarios/mirroring.md, docs/product/spikes/
// syncthing-mirroring.md) and reconciles it to a declarative desired state
// that casper_service computes from the trust framework's grants.
//
// Syncthing is a private child process: its own home under Casper's app
// support directory, its GUI/REST bound to localhost with a random API key,
// no global discovery, no public relays, no usage reporting. People never
// see it. Casper decides which peers and folders exist; Syncthing only moves
// the bytes.
//
// Everything Casper manages is namespaced: devices are named "casper:<...>"
// and folders have IDs starting "casper-", so reconciliation never touches
// anything else.
package mirror

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	FolderPrefix = "casper-"
	devicePrefix = "casper:"
	// History policy (decided 2026-10-01): every version for 7 days, then
	// one per day up to 30 days. Syncthing's staggered versioning keeps a
	// superset of that for 30 days; PruneVersions trims to the exact policy.
	historyMaxAge = 30 * 24 * time.Hour
)

// Peer is another machine's Syncthing device Casper has introduced.
type Peer struct {
	DeviceID  string   `json:"device_id"`
	Name      string   `json:"name"`
	Addresses []string `json:"addresses"`
}

// OwnedFolder is a real folder on this Mac, mirrored out encrypted.
type OwnedFolder struct {
	ID      string   `json:"folder_id"`
	Label   string   `json:"label"`
	Path    string   `json:"path"`
	Mirrors []string `json:"mirrors"` // device IDs
	Catcher string   `json:"catcher"` // device ID, or ""
	// Restore: this Mac is rebuilding the folder from its mirrors (a new or
	// replacement machine) -- receive-only, decrypting with the password.
	Restore bool `json:"restore"`
	// Password is never sent by casper_service; the daemon fills it in from
	// the Keychain (see PasswordStore).
	Password string `json:"-"`
}

// HeldFolder is someone else's folder this Mac keeps, encrypted.
type HeldFolder struct {
	ID    string `json:"folder_id"`
	Label string `json:"label"`
	// Owners are the owner's device IDs allowed to send this folder: their
	// usual Mac, plus a replacement Mac while it restores.
	Owners     []string `json:"owner_devices"`
	Kind       string   `json:"kind"` // "mirror" (versioned) | "catcher" (current gap only)
	QuotaBytes int64    `json:"quota_bytes"`
}

// Desired is the full state Casper wants this Mac's Syncthing in.
type Desired struct {
	Peers []Peer        `json:"peers"`
	Owned []OwnedFolder `json:"owned"`
	Held  []HeldFolder  `json:"held"`
}

// Manager owns the Syncthing child process and talks to its REST API.
type Manager struct {
	bin     string
	home    string // Syncthing's config + database
	dataDir string // where held (encrypted) folders and staging live
	listen  string // e.g. "tcp://0.0.0.0:22000"
	logf    func(string, ...any)

	mu       sync.Mutex
	cmd      *exec.Cmd
	guiAddr  string
	apiKey   string
	deviceID string
	client   *http.Client
}

func New(bin, home, dataDir, listen string, logf func(string, ...any)) *Manager {
	if listen == "" {
		listen = "default"
	}
	return &Manager{bin: bin, home: home, dataDir: dataDir, listen: listen, logf: logf, client: &http.Client{Timeout: 30 * time.Second}}
}

func randomKey() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func freeLocalPort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

// Start launches Syncthing (generating its identity on first run) and
// applies Casper's baseline options. Safe to call again after Stop.
func (m *Manager) Start() error {
	if err := os.MkdirAll(m.home, 0o700); err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(m.home, "cert.pem")); err != nil {
		out, err := exec.Command(m.bin, "generate", "--home="+m.home, "--no-port-probing").CombinedOutput()
		if err != nil {
			return fmt.Errorf("syncthing generate: %v: %s", err, out)
		}
	}
	port, err := freeLocalPort()
	if err != nil {
		return err
	}
	m.mu.Lock()
	m.guiAddr = fmt.Sprintf("127.0.0.1:%d", port)
	m.apiKey = randomKey()
	logFile, _ := os.OpenFile(filepath.Join(m.home, "syncthing.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	cmd := exec.Command(m.bin, "serve", "--home="+m.home, "--no-browser", "--no-upgrade",
		"--gui-address="+m.guiAddr, "--gui-apikey="+m.apiKey)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	cmd.Env = append(os.Environ(), "STNOUPGRADE=1", "STNORESTART=1")
	if err := cmd.Start(); err != nil {
		m.mu.Unlock()
		return fmt.Errorf("starting syncthing: %w", err)
	}
	m.cmd = cmd
	m.mu.Unlock()
	go func() { _ = cmd.Wait() }()

	deadline := time.Now().Add(30 * time.Second)
	for {
		var st struct {
			MyID string `json:"myID"`
		}
		if err := m.get("/rest/system/status", &st); err == nil && st.MyID != "" {
			m.mu.Lock()
			m.deviceID = st.MyID
			m.mu.Unlock()
			break
		}
		if time.Now().After(deadline) {
			return errors.New("syncthing didn't come up within 30s")
		}
		time.Sleep(300 * time.Millisecond)
	}
	return m.patch("/rest/config/options", map[string]any{
		"globalAnnounceEnabled": false, // casper_service is the discovery service
		"localAnnounceEnabled":  true,  // same-LAN peers find each other directly
		"relaysEnabled":         false, // until Casper runs its own relay
		"natEnabled":            true,
		"urAccepted":            -1,
		"crashReportingEnabled": false,
		"autoUpgradeIntervalH":  0,
		"listenAddresses":       []string{m.listenAddresses()},
		"startBrowser":          false,
	})
}

func (m *Manager) listenAddresses() string { return m.listen }

func (m *Manager) Stop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cmd != nil && m.cmd.Process != nil {
		_ = m.cmd.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _, _ = m.cmd.Process.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			_ = m.cmd.Process.Kill()
		}
	}
	m.cmd = nil
}

func (m *Manager) DeviceID() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.deviceID
}

// --- REST plumbing --------------------------------------------------------------

func (m *Manager) do(method, path string, body any, out any) error {
	m.mu.Lock()
	addr, key := m.guiAddr, m.apiKey
	m.mu.Unlock()
	if addr == "" {
		return errors.New("syncthing isn't running")
	}
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, "http://"+addr+path, r)
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", key)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := m.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("syncthing %s %s: %d %s", method, path, resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

func (m *Manager) get(path string, out any) error    { return m.do("GET", path, nil, out) }
func (m *Manager) put(path string, body any) error   { return m.do("PUT", path, body, nil) }
func (m *Manager) patch(path string, body any) error { return m.do("PATCH", path, body, nil) }
func (m *Manager) del(path string) error             { return m.do("DELETE", path, nil, nil) }
func q(s string) string                              { return url.PathEscape(s) }

// --- Addresses (Casper is the discovery service) -------------------------------

// Addresses returns where peers can reach this Mac: explicit tcp/quic
// addresses on every non-loopback interface (LAN, Tailscale, ...) on
// Syncthing's actual listen port, plus whatever public (NAT-mapped/STUN)
// addresses Syncthing has discovered. casper_service hands these to peers.
func (m *Manager) Addresses() []string {
	var st struct {
		ConnectionServiceStatus map[string]struct {
			LANAddresses []string `json:"lanAddresses"`
			WANAddresses []string `json:"wanAddresses"`
		} `json:"connectionServiceStatus"`
	}
	set := map[string]bool{}
	if err := m.get("/rest/system/status", &st); err == nil {
		for _, svc := range st.ConnectionServiceStatus {
			for _, a := range append(svc.LANAddresses, svc.WANAddresses...) {
				if a != "" && !strings.Contains(a, "0.0.0.0") && !strings.Contains(a, "[::]") {
					set[a] = true
				}
			}
		}
	}
	// Bound to one specific address (tests, or a deliberately pinned
	// listener): that address is the only one that works.
	if u, err := url.Parse(m.listen); err == nil && u.Hostname() != "" && u.Hostname() != "0.0.0.0" && u.Hostname() != "::" {
		set[m.listen] = true
		out := make([]string, 0, len(set))
		for a := range set {
			if !strings.Contains(a, "0.0.0.0") {
				out = append(out, a)
			}
		}
		sort.Strings(out)
		return out
	}
	port := "22000"
	for svc := range st.ConnectionServiceStatus {
		if u, err := url.Parse(svc); err == nil && u.Port() != "" {
			port = u.Port()
		}
	}
	ifaces, _ := net.Interfaces()
	for _, ifc := range ifaces {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			ipn, ok := a.(*net.IPNet)
			if !ok || ipn.IP.To4() == nil || ipn.IP.IsLinkLocalUnicast() {
				continue
			}
			set["tcp://"+net.JoinHostPort(ipn.IP.String(), port)] = true
			set["quic://"+net.JoinHostPort(ipn.IP.String(), port)] = true
		}
	}
	out := make([]string, 0, len(set))
	for a := range set {
		out = append(out, a)
	}
	sort.Strings(out)
	return out
}

// --- Reconciliation ------------------------------------------------------------------

type stDevice struct {
	DeviceID          string   `json:"deviceID"`
	Name              string   `json:"name"`
	Addresses         []string `json:"addresses"`
	Compression       string   `json:"compression,omitempty"`
	Introducer        bool     `json:"introducer"`
	AutoAcceptFolders bool     `json:"autoAcceptFolders"`
	Paused            bool     `json:"paused"`
}

type stFolderDevice struct {
	DeviceID           string `json:"deviceID"`
	EncryptionPassword string `json:"encryptionPassword"`
}

type stVersioning struct {
	Type             string            `json:"type"`
	Params           map[string]string `json:"params"`
	CleanupIntervalS int               `json:"cleanupIntervalS"`
}

type stFolder struct {
	ID               string           `json:"id"`
	Label            string           `json:"label"`
	Path             string           `json:"path"`
	Type             string           `json:"type"`
	Devices          []stFolderDevice `json:"devices"`
	FSWatcherEnabled bool             `json:"fsWatcherEnabled"`
	FSWatcherDelayS  float64          `json:"fsWatcherDelayS"`
	RescanIntervalS  int              `json:"rescanIntervalS"`
	Versioning       stVersioning     `json:"versioning"`
	Paused           bool             `json:"paused"`
	IgnorePerms      bool             `json:"ignorePerms"`
}

// HeldPath is where this Mac keeps another person's encrypted folder.
func (m *Manager) HeldPath(folderID string) string { return filepath.Join(m.dataDir, "held", folderID) }

// StagingPath is the owner-side folder of at-risk files shared with a
// catcher (see catcher.go).
func (m *Manager) StagingPath(folderID string) string {
	return filepath.Join(m.dataDir, "staging", folderID)
}

// CatchFolderID is the Syncthing folder that carries an owned folder's
// staging area to its catcher.
func CatchFolderID(folderID string) string { return folderID + ".catch" }

// Apply reconciles Syncthing to d: adds/updates Casper's devices and folders
// and removes Casper-managed ones that are no longer desired. Nothing outside
// the casper namespace is touched.
func (m *Manager) Apply(d Desired) error {
	self := m.DeviceID()
	wantDevices := map[string]stDevice{}
	for _, p := range d.Peers {
		if p.DeviceID == "" || p.DeviceID == self {
			continue
		}
		wantDevices[p.DeviceID] = stDevice{DeviceID: p.DeviceID, Name: devicePrefix + p.Name, Addresses: append(p.Addresses, "dynamic"), Compression: "metadata"}
	}
	wantFolders := map[string]stFolder{}
	for _, f := range d.Owned {
		if !strings.HasPrefix(f.ID, FolderPrefix) || f.Password == "" {
			continue
		}
		devs := []stFolderDevice{}
		for _, id := range f.Mirrors {
			if _, ok := wantDevices[id]; ok {
				devs = append(devs, stFolderDevice{DeviceID: id, EncryptionPassword: f.Password})
			}
		}
		typ := "sendonly" // the owner's copy is the truth; mirrors never write back
		if f.Restore {
			typ = "receiveonly"
		}
		if err := os.MkdirAll(f.Path, 0o755); err != nil {
			return err
		}
		wantFolders[f.ID] = stFolder{ID: f.ID, Label: f.Label, Path: f.Path, Type: typ, Devices: devs,
			FSWatcherEnabled: true, FSWatcherDelayS: 1, RescanIntervalS: 3600, Versioning: stVersioning{Type: "", Params: map[string]string{}}}
		if f.Catcher != "" {
			if _, ok := wantDevices[f.Catcher]; ok {
				sp := m.StagingPath(f.ID)
				if err := os.MkdirAll(sp, 0o700); err != nil {
					return err
				}
				// Normally staging is sent to the catcher. While restoring, the
				// catcher's gap is received into staging instead, then laid
				// over the restored folder (OverlayCatcher).
				catchType := "sendonly"
				if f.Restore {
					catchType = "receiveonly"
				}
				wantFolders[CatchFolderID(f.ID)] = stFolder{ID: CatchFolderID(f.ID), Label: f.Label + " (catcher)", Path: sp, Type: catchType,
					Devices: []stFolderDevice{{DeviceID: f.Catcher, EncryptionPassword: f.Password}}, FSWatcherEnabled: true, FSWatcherDelayS: 1,
					RescanIntervalS: 600, Versioning: stVersioning{Params: map[string]string{}}}
			}
		}
	}
	for _, h := range d.Held {
		owners := []stFolderDevice{}
		for _, o := range h.Owners {
			if _, ok := wantDevices[o]; ok {
				owners = append(owners, stFolderDevice{DeviceID: o})
			}
		}
		if len(owners) == 0 || !strings.HasPrefix(h.ID, FolderPrefix) {
			continue
		}
		id, label := h.ID, h.Label
		versioning := stVersioning{Type: "staggered", Params: map[string]string{"maxAge": fmt.Sprint(int(historyMaxAge.Seconds()))}, CleanupIntervalS: 3600}
		if h.Kind == "catcher" {
			id, label = CatchFolderID(h.ID), h.Label+" (catcher)"
			versioning = stVersioning{Params: map[string]string{}} // a catcher holds only the current gap
		}
		p := m.HeldPath(id)
		if err := os.MkdirAll(p, 0o700); err != nil {
			return err
		}
		// Quota: the owner's side checks size before mirroring starts; this
		// side pauses a held folder that has outgrown its grant by >10%.
		paused := h.QuotaBytes > 0 && DirSize(p) > h.QuotaBytes+h.QuotaBytes/10
		wantFolders[id] = stFolder{ID: id, Label: label, Path: p, Type: "receiveencrypted",
			Devices: owners, FSWatcherEnabled: false, RescanIntervalS: 3600, Versioning: versioning, Paused: paused}
	}

	var haveDevices []stDevice
	if err := m.get("/rest/config/devices", &haveDevices); err != nil {
		return err
	}
	var haveFolders []stFolder
	if err := m.get("/rest/config/folders", &haveFolders); err != nil {
		return err
	}
	// Devices first (folders reference them), then folders; removals last.
	for id, dev := range wantDevices {
		if err := m.put("/rest/config/devices/"+q(id), dev); err != nil {
			return err
		}
	}
	for id, f := range wantFolders {
		if err := m.put("/rest/config/folders/"+q(id), f); err != nil {
			return err
		}
	}
	for _, f := range haveFolders {
		if strings.HasPrefix(f.ID, FolderPrefix) {
			if _, ok := wantFolders[f.ID]; !ok {
				if err := m.del("/rest/config/folders/" + q(f.ID)); err != nil {
					return err
				}
			}
		}
	}
	for _, dev := range haveDevices {
		if strings.HasPrefix(dev.Name, devicePrefix) {
			if _, ok := wantDevices[dev.DeviceID]; !ok {
				if err := m.del("/rest/config/devices/" + q(dev.DeviceID)); err != nil {
					return err
				}
			}
		}
	}
	return nil
}
