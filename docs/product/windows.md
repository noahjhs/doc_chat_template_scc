# Casper for Windows: inventory, phases, test plan

Written 2026-10-16. Goal: a Windows app at parity with the Mac for the pilot.
This is an inventory and a plan; nothing was installed and no code changed.

## Summary

- The Go agent is close to *compiling* on Windows and far from *working*.
  `agent/cmd/casper` cross-compiles to `windows/amd64` and `windows/arm64`
  with **one** source fix (below). But every macOS-only piece is already
  stubbed with a silent no-op or an error, so the binary would start, show a
  tray icon (maybe), and do nothing useful: no pairing hand-off, no secrets
  storage (mirroring refuses), no folder picker, no login item.
- The work is mostly **native glue** (about 20 small pieces, each smaller
  than its macOS twin) plus **packaging and signing**, and a **test rig**
  (Tart can't run Windows). The core (Syncthing management, restore,
  policy, crypto, tunnel, server) is portable Go and needs only a few
  path fixes.
- The three things that need a decision, not just work: where secrets live
  and how much weaker that is than Keychain (see "Secrets"); how the browser
  hands a pairing to the running app (single instance); and how the Windows VM is
  driven (see "E2E plan").

## 1. Cross-compile result

Run from `agent/` with the Go 1.27.1 toolchain on this Mac:

```
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build ./cmd/casper
GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build ./cmd/casper
```

- **As committed: fails**, both architectures, with one error:
  `internal/mirror/catcher.go:76: undefined: syscall.Stat_t` (`sameFile`
  compares device and inode on Unix). Go reports only the first failing
  package, so I re-ran with that function replaced by `os.SameFile` (which is
  all the fallback line below it already does) through `go build -overlay`,
  with no change to the tree. Result: **both build** with no further errors
  (`casper.exe` about 16 MB amd64, 15 MB arm64; `file` says PE32+ x86-64 and
  Aarch64). `casper-testdaemon` and `go vet ./...` (tests included) are clean
  under the same overlay for windows/amd64.
- Fix for Phase 0: delete the `syscall.Stat_t` branch in `sameFile` (or move
  it to a `//go:build !windows` file). `os.SameFile` already does the right
  thing on every platform.
- No cgo is needed on Windows. The Mac build uses cgo for the status icon
  (`getlantern/systray`), the URL handler and Keychain; none of that is in
  the Windows build, so cross-compiling from the Mac needs no C toolchain.
- "Builds" is not "runs". Everything below is what the stubs hide.
- Not checked: `go test` on Windows (needs a Windows host; the e2e rig will
  run it), and the Windows-only warnings `go vet` gives for the amd64 target
  only (none appeared).

## 2. macOS-only pieces and their Windows equivalents

Status: **stub** = compiles, silently does nothing or returns an error;
**partial** = a Windows file exists but isn't real; **missing** = no hook at all.

| # | Piece | Mac today | Windows today | Windows equivalent / plan |
|---|---|---|---|---|
| 1 | Session secrets (`config/session_keychain_darwin.go`, service "Casper Session") | login Keychain; per-app ACL | **falls back to plaintext `session.json`** (`session_store_other.go`) | Sealed store, see "Secrets" below. Today's fallback leaves the device token and command key readable by any process of the user. |
| 2 | Account password and token (`setup/keychain_darwin.go`, "Casper Account") | Keychain | stub: errors "setup is macOS-only" | same sealed store |
| 3 | Mirror folder passwords (`mirrord/keychain_darwin.go`, "Casper Mirror Keys", incl. `ListAccounts`) | Keychain | stub: errors, so **mirroring cannot start** | same sealed store; `ListAccounts` = list the store's names |
| 4 | Master key (`recovery/keychain_darwin.go`, "Casper Master Key") | Keychain | stub: errors | same sealed store |
| 5 | Backup keys (`backup/keychain_darwin.go`, "Casper Backup Keys") | Keychain | stub: errors | same sealed store |
| 6 | Dialogs: confirm with a hidden field, yes/no with timeout, folder panel, multi-select list, open/save panel, farewell, info/error (`dialog/dialog_darwin.go`, all AppleScript/JXA via `osascript`) | native | **partial**: only fatal error, error, "don't ask again" and info exist (PowerShell MessageBox). `Confirm`, `ChooseFolder`, `ChooseFromList`, `SaveFile`, `OpenFile` return "no answer"; the farewell is a no-op. These are exactly what consent, folder choice, restore picker and recovery kit use. | Small native helper. Best: call Win32 directly from Go (no PowerShell, no console flash): `IFileOpenDialog` with `FOS_PICKFOLDERS` (folder), `IFileSaveDialog`/`IFileOpenDialog` (kit and restore files), `TaskDialogIndirect` (yes/no, timeout via `TDF_CALLBACK_TIMER`, "don't ask again" via the verification checkbox), and a small custom dialog or `TaskDialog` with radio buttons for the multi-select list (a real list box needs a dialog template; or a WebView-free HTML-less fallback). Hidden password field: `CredUIPromptForWindowsCredentials` or a small dialog template with `ES_PASSWORD`. Use `golang.org/x/sys/windows` (already an indirect dependency) plus COM through `syscall`; or a pure-Go library such as `github.com/ncruces/zenity`, which wraps exactly these dialogs and runs without cgo. Recommend trying `zenity` first and replacing only what it can't do. |
| 7 | Hidden-input prompt for `setup account` (`setup/setup.go` `askHidden`, osascript with `hidden answer`) | native | missing (calls osascript, fails) | same password dialog as row 6; must not echo to the agent's pipe |
| 8 | Open a URL (`exec.Command("open", url)`: `daemon.go` ×2, `setup/setup.go` `openURL`) | `open` | missing: no `open` on Windows | `rundll32 url.dll,FileProtocolHandler <url>` (or `ShellExecute`); one `openURL` helper in a `platform` package |
| 9 | Reveal in Finder (`main.go` `open -R`), open a folder (`commands/mirror_actions.go` `open dir`) | `open` | missing | `explorer.exe /select,"<path>"` (note: explorer returns exit code 1 even on success); `explorer.exe "<dir>"` |
| 10 | URL scheme `casper://` (`urlscheme/*.m`, Apple Event, `CFBundleURLTypes`) | LaunchServices delivers an event to the running app | stub: `Register` returns nil, nothing ever arrives | Registry protocol handler under `HKCU\Software\Classes\casper` (`URL Protocol`, `shell\open\command` = `"...\Casper.exe" "%1"`). Windows **starts a new process** for each link, so the new process must hand the URL to the running one and exit: single-instance via a named mutex plus a named pipe (or a POST to the daemon's own `127.0.0.1` port). Per-environment schemes (`casper`, `casper-dev`, `casper-test`) stay: each build registers its own key. Register on every start (cheap, self-heals after a move). |
| 11 | Login item (`config/loginitem_darwin.go`: LaunchAgent plist; Windows file is a stub that reports false) | LaunchAgent, refreshes path on move | **partial**: stub | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `Casper` (per-user, no admin). `IsLoginItem` reads it, `RefreshLoginItem` rewrites it if the path changed. Windows 10/11 also shows it in Settings > Apps > Startup (and the user can turn it off there via `StartupApproved`; read that key too so the menu checkbox tells the truth). |
| 12 | Move to Applications (`cmd/casper/moveapp_darwin.go`: ditto, strip quarantine, relaunch) | yes | no-op (`moveapp_other.go`) | Not needed if there is an installer. With a per-user installer to `%LOCALAPPDATA%\Programs\Casper`, nothing to move. If we ship a bare zip (not recommended), copy to that folder and relaunch, same as the Mac. Strip Mark-of-the-Web from the copy (`Zone.Identifier` stream) so SmartScreen isn't asked twice. |
| 13 | Tray icon (`getlantern/systray` + `statusicon_darwin.go` for the coloured dot in the menu) | menu bar, cgo AppKit | `systray` supports Windows (no cgo); `statusicon_other.go` no-ops the coloured dot | Keep `systray` for the menu. Windows hides new tray icons in the overflow flyout by default; the first run should say where it is (a toast, or a one-time dialog). The coloured dot becomes a menu-item icon (systray can set one on Windows; PNG is converted to a bitmap) or a tooltip text; status in the tray icon itself (green/grey ghost) is the more native choice. Light/dark tray: use a coloured ghost (works on both). |
| 14 | Re-focus browser after pairing (`activate/activate_darwin.go`, reads LaunchServices plist, AppleScript) | yes | no-op | Not needed: after `open` the browser is already foreground, and Windows blocks focus stealing anyway. Keep the no-op. |
| 15 | Single instance / second launch | LaunchServices | none: a second launch starts a second daemon (port 8000 clash, two Syncthings on one home) | named mutex (see 10); second instance forwards its argument and exits. Also needed for `Restart` (`restartApp` spawns the new process before the old exits; on Windows keep the existing "shut down first" order). |
| 16 | Syncthing bundle (`build_go_macos.sh`: `syncthing-macos-<arch>-v2.1.5.zip` into `Contents/MacOS/syncthing`; found next to the executable in `cmd/casper/mirroring.go`) | bundled | not bundled; mirroring is off when the binary is missing | `syncthing-windows-{amd64,arm64}-v2.1.5.zip` from the same release, same pinned SHA-256 check, `syncthing.exe` next to `Casper.exe`. Path lookup must add `.exe`. Launch it without a console window: set `SysProcAttr{HideWindow: true, CreationFlags: CREATE_NO_WINDOW}` in `mirror/manager.go` (all three `exec.Command` calls). Make it die with the parent: put it in a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, since Windows doesn't orphan-kill (a crash would leave Syncthing holding the home directory lock). |
| 17 | Windows Firewall | none (macOS asks "find devices on local networks"; the code already avoids that by turning off local discovery and NAT-PMP) | n/a | Syncthing listens on TCP/QUIC 22000 (`listen` = `"default"` unless set) and would trigger the Windows Defender Firewall "allow access" prompt for `syncthing.exe` on first start, which also needs admin to answer on a standard account. Options: (a) make direct listening optional and rely on dialing out plus Casper's relay, so Syncthing binds nothing inbound (**preferred for the pilot**: set `listenAddresses` to only the relay addresses; check this keeps NAT-less peers reachable, since `mirroring.relays()` already hands the Macs the relay); (b) have the installer add an inbound rule for `syncthing.exe` (needs the elevated installer step). The prompt itself is a blocking, scary dialog for a non-technical friend and is the single biggest new-user-experience risk on Windows. |
| 18 | Sandbox for shell commands (`commands/seatbelt_darwin.go`, `sandbox-exec`) | Seatbelt profile | `seatbelt_other.go`: plain `exec.Command` | Not parity-critical (the argv allowlist is the real boundary). Job Object + restricted token / AppContainer later. For the pilot, `run_shell_command` should be reported unavailable on Windows (see section 3) rather than run unsandboxed. |
| 19 | Disk space (`mirror/disk_unix.go`; Windows returns "infinite") | `Statfs` | stub: restore never sees a full disk | `GetDiskFreeSpaceEx` via `x/sys/windows`. Matters: restore's copy-vs-replace choice and the "needs X, you have Y" message depend on it. |
| 20 | Build and packaging (`build/build_go_macos.sh`: .app, Info.plist, icns, Developer ID, notarize, staple, zip, plus embeds of domains and ghost.png) | script | none | New `build/build_go_windows.sh` (runnable from this Mac, `CGO_ENABLED=0`), see section 5. |
| 21 | `setup` commands (`setup/*.go`, run by the person's agent as `Casper.app/Contents/MacOS/Casper setup ...`) | the agent calls the app binary | the code compiles, but: Keychain (rows 2 to 5), `openURL`, the password dialog, `appBundle` (looks for `.app/`), recovery-kit default dir `kitDefaultDir("/Volumes")` | See "Setup commands" below. |
| 22 | `onboarding/AGENTS.md` (served at `/agents.md` and `/onboarding.zip`) | `CASPER=/Applications/Casper.app/Contents/MacOS/Casper`, `curl ... \| unzip ... -d /Applications && open ...` | macOS only text | A Windows variant of the bootstrap (PowerShell), selected by the agent from its own OS, or two files served by the same endpoint. See section 4. |
| 23 | Dev tooling that assumes a Mac (`tools/scenario.py` `tart exec zsh`, `osascript`, `pkill`) | yes | n/a | Windows personas need their own step functions (see section 6). |

### Secrets

Five Keychain items, all under one small interface (service name + account
-> bytes). Windows has no per-application ACL: Credential Manager and DPAPI
both unlock for any process running as the same user. On the Mac the Keychain
item is readable by Casper without a prompt but a script gets an "allow
access?" prompt that only the person can answer. That property is what the
comment in `session.go` relies on to stop an agent with a shell (the person's
own Claude Code) from impersonating the daemon, for example to forge consent.
**On Windows that protection does not exist.** Anything stronger (a separate
Windows service running as `LocalSystem` or a virtual service account that
holds the secrets, with the user-level tray app talking to it over a pipe
whose ACL we control) is a real project, and still doesn't stop an agent
that is itself admin.

Recommendation for the pilot: **DPAPI-sealed files** (`CryptProtectData`
with the current-user scope) under `%LOCALAPPDATA%\Casper\secrets\<service>\<account>`,
behind one `secretstore_windows.go` that all five consumers share. Reasons:
no 2.5 KB blob limit (Credential Manager caps a credential at 2560 bytes,
and the session list grows with the number of paired accounts); one helper,
testable; same strength as Credential Manager. Credential Manager has one
advantage (items are visible and deletable in Control Panel); not worth the
size limit. Either way, state this plainly in the trust docs: on Windows a
program running as the user can read Casper's keys. Record the decision in
`docs/product/risk-model.md` before the pilot invites anyone.

Also: `config.AppConfigDir()` uses `os.UserConfigDir()`, which is
`%APPDATA%` (**Roaming**) on Windows. The Syncthing home (index database),
mirror staging and logs live under it. On a domain-joined PC that roams and
the Syncthing DB would be copied around at logon; on any PC it is the wrong
place for a cache. Use `%LOCALAPPDATA%` on Windows (`os.UserCacheDir()` is
that, or call `SHGetKnownFolderPath(FOLDERID_LocalAppData)`); keep
`Casper`, `Casper-dev`, `Casper-test` as the directory names.

### Setup commands (what an agent calls)

- They run the app's own binary. On Windows the agent runs
  `& "$env:LOCALAPPDATA\Programs\Casper\Casper.exe" setup status`. A
  tray app built with `-H=windowsgui` has no console, so stdout from a parent
  that passes pipes still works, but a person typing in a terminal sees
  nothing. Fix: on `setup`, call `AttachConsole(ATTACH_PARENT_PROCESS)` when
  there are no inherited pipes. (Alternative: ship a console-subsystem
  `casper-cli.exe` next to it; larger, and a second signed binary.) The
  one-shot `setup` mode must still return before any tray or listener starts
  (already true).
- `setup pair` calls `openURL(casper://pair?...)`. That needs rows 8 and 10.
  `setup` also waits for `config.LoadLastPairingResult`, which is a file and
  is fine.
- `setup agent` runs `claude mcp add ...`: on Windows `claude` is
  `claude.cmd`/`claude.exe` on PATH; `exec.Command("claude")` finds `.exe`
  and `.cmd` through `LookPath` with `PATHEXT` since Go 1.19. Test it. Claude
  Code on native Windows needs Git for Windows today; the onboarding text
  must not assume a POSIX shell.
- `setup recovery-kit` and `setup recover` write
  `Casper Recovery Kit (<user>).txt` in `Documents` by default and offer
  external drives starting from `/Volumes` (`kitDefaultDir`). On Windows:
  enumerate removable and fixed non-system drives (`GetLogicalDrives` +
  `GetDriveType`), skip `%SystemDrive%`, default to the Desktop. Documents
  may be OneDrive-redirected; use `SHGetKnownFolderPath(FOLDERID_Documents)`
  rather than `home + "/Documents"` in `setup.go:documentsDir` and
  `kitplace.go`. The "kit is inside a mirrored folder" check (`insideAny`)
  must compare case-insensitively and across `\` and `/` on Windows.
- `appBundle()` looks for `.app/`; on Windows return the executable path.

## 3. Beyond the stubs: things that compile but behave differently

- **Home confinement and folder names.** The daemon is confined to
  `os.UserHomeDir()` (`C:\Users\<name>`); `Documents`, `Desktop`, `Pictures`
  are *redirected* to OneDrive for many Windows users (known-folder move), so
  the real folder is `C:\Users\<name>\OneDrive\Documents`, which is inside
  home but not at `home\Documents`. The guide suggests "Documents" by name:
  the daemon must resolve it with the known-folder API, and the picker should
  show what the person sees. OneDrive "files on demand" placeholders
  (cloud-only files) look like files to Go but can't be read without
  downloading them; Syncthing will count them as unreadable or trigger
  downloads. Needs a decision: skip placeholders and say so (the existing
  `unreadable_items` honesty rule would apply, and a folder with them is never
  "protected").
- **`~` and path shapes.** `commands.resolvePath` expands `~/` only; it does
  not accept `~\`. The server rewrites paths with `re.sub(r"^/Users/[^/]+", "~", ...)`
  (`casper_service/main.py:2996`, `_short_path`), so Windows paths
  (`C:\Users\Sam\...`) would leak the username into the guide's replies (the
  guide forbids that). Defaults like `~/Casper Restores/<label>`
  (`main.py:3418,3452,3476`) are strings sent to the daemon, which must treat
  `/` as a separator on Windows. Treat both as server+agent changes with a
  parity test.
- **Windows filesystem rules**, which Syncthing mostly handles but our own
  code does not: case-insensitive names (two files differing only in case
  can't coexist: restore into a "dated copy next to the original" is fine,
  mirror clones from a Mac may collide); reserved names (`CON`, `NUL`,
  trailing dots and spaces); the 260-character limit unless the manifest sets
  `longPathAware` (add it to the embedded manifest, and Syncthing handles its
  own long paths); `:` in file names (the dated-copy names in `restore.go`
  and `versions.go` use `20060102-150405`, no colons, good; the kit and
  restore folder names such as `Documents (as of 2026-10-10)` are fine).
  Syncthing's `.stversions` and `.stfolder`, `.stignore`: hidden attribute
  handling is Syncthing's; `restore.go` treats `.syncthing.*` and `.DS_Store`
  as noise, so add `Thumbs.db`, `desktop.ini`, `~$*`.
- **Open or locked files.** Windows locks files that Word and Outlook have
  open, and Syncthing reads them through shared access but can fail. Windows
  has Volume Shadow Copy; Syncthing doesn't use it. Expect more "can't read"
  items than on a Mac; the honesty rule (never "protected" with unreadable
  items) already covers it, but the guide's "unreadable" step wording says
  Mac things ("Full Disk Access") only if you read it carefully; check.
- **`.photoslibrary`** carve-out is Mac-only and harmless. A Windows
  equivalent (Photos app has no library bundle) is not needed.
- **Restore.** `restore.go` uses `os.Rename` and falls back to copy; rename
  over an existing file fails on Windows if the target is open, and renaming
  a directory that Explorer has open fails. The `.casper-restoring` temp
  approach is right; add retry with backoff on `ERROR_SHARING_VIOLATION`.
  File modes (`0o600`, `0o700`) are ignored; this is not a regression, but
  the "private by mode" assumptions in `mirror/trailer.go` and `catcher.go`
  (held ciphertext staged `0o700`) don't hold: put those under
  `%LOCALAPPDATA%\Casper` (user-only ACL by default), not under Documents.
  `catcher.go` `sameFile` is the Phase 0 fix.
- **`run_shell_command` and policy.** The daemon's rules are POSIX shaped
  (`PathResolutionPATH`, `man -w`, `$PATH`, binaries like `git`/`python3`;
  `PathResolutionManPath` shells out to `man`, which doesn't exist). The
  product direction already de-emphasises the shell tool; for Windows the
  daemon should answer `run_shell_command` with a clear "not available on
  this computer" `deny` and the MCP server should not list Windows hosts as
  shell targets. Keep the file/backup/mirror/restore actions (the v1
  scenario). Decide whether the policy-layer actions are exposed at all.
- **Signals and lifecycle.** `syscall.SIGTERM` exists in Go on Windows but
  never fires from a normal shutdown. Windows tells a GUI app to close with
  `WM_ENDSESSION`/`WM_QUERYENDSESSION`; the tray library's message loop sees
  it, but we should confirm `systray.Quit()`'s path runs `shutdownDaemon`
  (stops Syncthing) at logoff and shutdown. Test by logging off.
- **Sleep, network change, metered connections.** Same as the Mac in kind;
  Syncthing handles reconnects. "Modern Standby" laptops keep running briefly
  at night and then sleep; the nightly mirror window will be shorter than on
  a Mac mini. Worth a note in the pilot expectations, not code.
- **Antivirus.** An unsigned Go binary that writes the Run key, registers a
  protocol handler and spawns another unsigned binary is a classic
  false-positive profile for Defender. Signing (section 5) is not optional.
- **Encoding and PowerShell.** The existing `dialog_windows.go` shells out
  to PowerShell with the message inlined and `-Command`; PowerShell start-up
  is slow (~0.5 to 1 s), flashes a window unless hidden, and some
  organisations block it. Replace with direct Win32 (row 6).

## 4. What the site and the guide assume about macOS

Nothing in `casper_service` has a notion of the host's operating system:
there is no platform column on hosts or pairings (`hostname` is all the
pairing sends). Decide first whether to add one (`platform`: `macos`|`windows`
sent at pair time and in presence). It is cheap and lets the guide, the MCP
instructions and the folder defaults say the right thing per host. Without
it, a person with a Mac and a Windows PC gets Mac wording for both.

**Site (`site/www/`)**

- `download.html`: single "Download Casper for Mac" link, `Casper.zip`,
  "signed and notarized", "click Open if macOS asks", "menu bar". Footers of
  every page say "Early alpha · macOS". `about-app.html`: "lives in your menu
  bar", "drag Casper from Applications to the Trash", "Signed by its
  developer and notarized by Apple". Needs per-OS blocks, chosen from the
  browser (`navigator.userAgentData.platform` or UA; Windows on ARM reports
  as Windows) with a manual switch.
- `signin.html` / `signup.html` / `app.js`: "Sign in on this Mac",
  "Connecting this Mac…", "Lost your Mac?", "I lost my Mac" (and the
  `lost-mac` flag name in the API); the `casper://pair` link (works as is with
  the registry handler) and the first-launch code flow (`?from=app&pair=code`,
  which needs no URL scheme and is the safer path on Windows too). Browsers
  show their own "Open Casper?" prompt for custom schemes on Windows
  (Chrome/Edge: "Open Casper" with an "Always allow" box) which the code flow
  avoids; good that it is the default.
- `guide.html`: "If your Mac asks whether Casper can see X, choose Allow"
  (a macOS privacy prompt; Windows has none; remove for Windows), "Chrome
  blocked it? ... Download suspicious file" (on Windows it is the SmartScreen
  "Windows protected your PC / Run anyway" and Edge's "Keep" flow: different
  text), and the `dots("mac")` busy indicator (internal name only).
- `index.html`, `invite.html`: marketing copy ("Sam's Mac mini", "on your Mac").

**Guide and MCP text (`casper_service/guide.py`, `mcp_server.py`, `main.py`)**

- `VOICE`/`INSTRUCTIONS` both say "macOS only; early alpha" and use "Mac"
  throughout ("20 GB on this Mac?", "Riley's Mac"). `guide.py` stages: install
  ("Their Mac isn't connected yet"), recovery ("In Casper's menu (the ghost in
  the menu bar), choose Recover from a Lost Mac"; on Windows it is the tray,
  bottom-right, possibly in the overflow). `guide_eval.py` scripts compare
  these strings; add Windows variants to the eval before changing prompts.
  Suggest "this computer" as the neutral word everywhere, with "Mac" /
  "PC" used when the host's platform is known. Keep the voice rules.
- Download URL: `GET /download/casper/macos` and `/download/casper-macos.zip`
  serve `Casper-macos.zip` from `DIST_DIR`, `GUIDE_DOWNLOAD_URL` defaults to
  the macOS URL (`main.py:3534`). Add `/download/casper/windows` (arm64 or
  amd64 by `?arch=`, and a default chosen from the User-Agent client hints;
  ARM Windows runs the amd64 build under emulation, so a single amd64
  installer is an acceptable first release, but native arm64 is the better
  default on ARM hardware). All `Cache-Control: no-store`, like the Mac one.
- `onboarding/AGENTS.md` (and `/agents.md`, `/onboarding.zip`): Mac-only
  paths and `curl | unzip | open`. Add a Windows section (PowerShell:
  `Invoke-WebRequest`, run the installer silently, start `Casper.exe`).
- Server paths and wording: `_short_path` (`/Users/...`), "Casper Restores"
  (default `~/Casper Restores`), "free space on your Mac" strings
  (`main.py:3155-3160`, `3301`, `3309`, `3814`), restore-picker "native list"
  (`mirror_restore_*` actions, which depend on dialog row 6), `mailer.py` copy,
  `mirroring.py` consent texts, `trust.py`. Telegram messages say "Mac".
- Everything the browser or agent does through the MCP server and
  casper_service works the same: the protocol between daemon and server is
  JSON over HTTPS and the tunnel.

## 5. Build, signing and distribution

- **Build.** `build/build_go_windows.sh` (new, runnable on this Mac): same
  embed staging as the Mac script (`auth_server*.txt`, `relay_server*.txt`,
  `app_server*.txt`, ghost); `GOOS=windows GOARCH=amd64|arm64 CGO_ENABLED=0
  go build -trimpath -ldflags "-H=windowsgui -s -w" -o Casper.exe ./cmd/casper`;
  an embedded resource file for the icon, version info and a manifest
  (`github.com/tc-hib/go-winres` or `goversioninfo`, run before `go build`):
  `longPathAware`, `supportedOS` Windows 10/11, `dpiAware`, and
  `requestedExecutionLevel asInvoker` (never ask for admin). Fetch
  `syncthing-windows-<arch>-v2.1.5.zip` with the same checksum check against
  `sha256sum.txt.asc`. Per-environment variants (`prod`/`dev`/`test`) set the
  product name ("Casper", "Casper Dev", "Casper Test"), the URL scheme and the
  registry value names, exactly as the Mac script sets bundle ID and scheme.
- **Package.** A per-user installer, no admin: **MSIX** (clean install and
  uninstall, protocol and startup declared in the manifest, signed with the
  same certificate; but requires either the Store or a sideloading-trusted
  cert and App Installer, which a normal friend won't have) versus **Inno
  Setup / WiX .exe or .msi** installing to `%LOCALAPPDATA%\Programs\Casper`
  (writes the Run key and protocol handler, adds an optional firewall rule,
  creates the uninstaller entry). Recommend an **Inno Setup `.exe`**
  (builds on a Mac via the `iscc` compiler under Wine or in CI, small, silent
  switch `/VERYSILENT` for the agent's bootstrap). Also offer a bare
  `Casper-windows-<arch>.zip` for the agent flow if the installer is awkward
  to script. The uninstaller must stop Casper and Syncthing, remove the Run
  key and protocol key, and leave the user's files and the mirrored folders
  alone (Casper's data directory is removed only if the person says so;
  friends' copies are deleted after 7 days as the site says).
- **Signing.** Authenticode. A plain OV certificate no longer gives instant
  SmartScreen reputation; since 2024 EV and OV are treated the same by
  SmartScreen and reputation accrues per file hash/certificate with downloads.
  Options, in order of fit for a pilot of a few dozen people: **Microsoft
  Trusted Signing (Azure Artifact Signing)** (about $10/month, short-lived
  certificates, requires an organisation with 3+ years of verifiable history
  or an individual developer validation in supported regions; check whether
  we qualify), or an OV/EV certificate from a CA with a cloud HSM (DigiCert
  KeyLocker, SSL.com eSigner; keys can't be exported since 2023, so signing
  runs through the vendor's tool, which has a macOS/Linux CLI via
  `jsign`). Sign both `Casper.exe`, `syncthing.exe` (re-sign ours; Syncthing
  signs its own releases but with the project's key, which is fine to leave)
  and the installer; timestamp every signature (`/tr` RFC 3161) so signatures
  outlive the certificate. Expect a SmartScreen "unrecognized app" prompt on
  early downloads whatever we do; the guide's "Ignore browser warnings"
  line should say how to click "More info > Run anyway" with a picture, as the
  Mac one will for Gatekeeper. Pilot users are invited, so this is acceptable;
  plan to submit the file to Microsoft for malware analysis (Defender false
  positives) the first time.
- **Update path.** Mac updates are "download the new zip". Same for Windows
  for the pilot (installer over the top, running instance is stopped by the
  installer via a mutex check). An auto-update is later.
- **Syncthing licence.** MPL-2.0; same obligations as the Mac bundle.

## 6. Landable phases

Each phase is independently landable through `tools/land.sh` (it runs
`go test ./...` when `agent/` changes, on this Mac; Windows-only files need
`GOOS=windows go vet` in addition, see phase 0) and none breaks the Mac build.

**Phase 0: make it build and keep it building (about half a day).**
Fix `sameFile`. Add a CI-style check to `tools/land.sh`: `GOOS=windows
GOARCH=amd64 CGO_ENABLED=0 go vet ./...` and a `GOARCH=arm64` build, so Mac
changes can't re-break it. Make `AppConfigDir` use `%LOCALAPPDATA%` on
Windows. Add `platform` (`macos`/`windows`) to what `POST /hosts/pair` and
presence send and store it on the host (additive, nullable); expose in
`list_hosts`/`my_casper`. Nothing user visible.

**Phase 1: platform package and secrets (about 2 days).**
One `internal/platform` package for `OpenURL`, `Reveal`, `OpenFolder`,
`KnownFolder(Documents|Desktop|…)`, `FreeBytes`, `HideWindow(cmd)`, replacing
the six scattered `exec.Command("open"...)` calls (a pure refactor on the
Mac). `secretstore_windows.go` (DPAPI) behind the five Keychain interfaces,
with unit tests that run on Windows in the e2e VM (the Mac CI can only
compile them). The risk decision on weaker secret protection is written into
`risk-model.md`.

**Phase 2: shell: single instance, URL handler, login item, tray (about 3 days).**
Mutex and pipe single instance; registry protocol handler written on start;
the second-process forwarder; Run-key login item with `StartupApproved`; tray
icon colours and the "where is Casper" first-run notice; `AttachConsole` for
`setup`; `-H=windowsgui`. After this, `Casper.exe setup status` and
`setup pair` work from an agent, and a `casper://pair` link reaches the
running app. First time the e2e rig is needed (phase 2 cannot be tested from
the Mac).

**Phase 3: dialogs and pickers (about 3 days).**
Row 6 and 7: `Confirm` with timeout, folder chooser, multi-select list, open
and save panel, hidden password, farewell, kit save location, `Reveal`.
This unblocks consent, mirroring start, the restore picker, the recovery kit
and the recovery flow. Parity tests: the existing `dialog` call sites don't
change; add a `CASPER_DIALOG_SCRIPT` env hook for tests (an answer file) so
e2e runs on Windows can answer dialogs without VNC clicks when they are not
the thing under test.

**Phase 4: mirroring end to end (about 3 days).**
Bundle `syncthing.exe`, hidden window, Job Object, firewall decision (listen
only on relay/outbound), known-folder resolution for "Documents" and
OneDrive handling, locked-file and placeholder behaviour, rename retries,
restore on NTFS, `FreeBytes`. `casper_service` path shortening and the
`/Users/` regex; `~\` in `resolvePath`. Mixed pair: a Mac and a Windows PC
mirror to each other (the test that matters: Syncthing across OSes, case
collisions, long paths).

**Phase 5: recovery v2 and restore on Windows (about 2 days).**
Master key and legacy bundle in the secret store; kit v3 import/export with
drive enumeration and OneDrive-aware default; `mirror_restore_plan`
(free space), `restore_version`, `restore_as_of`, `show_restore_picker`
(native list). A kit made on a Mac must restore on Windows and the reverse
(path separators and `legacy_bundles`). Re-run the `lost-mac` scenario
with a Windows target.

**Phase 6: packaging, signing, distribution (about 3 days plus lead time on
the certificate, which is the long pole; start applying in phase 0).**
`build_go_windows.sh`, installer, Authenticode, `/download/casper/windows`,
`GUIDE_DOWNLOAD_URL` by platform, `onboarding/AGENTS.md` Windows section,
site download and about pages with OS detection.

**Phase 7: site, guide and agent wording (about 2 days, can start after phase 0).**
"This computer" wording, per-platform steps and screenshots, the
SmartScreen note replacing the Chrome-blocked note on Windows, guide eval
additions, the Telegram and email copy. Remove "macOS only" from
`VOICE`/`INSTRUCTIONS` only when phase 5 passes e2e.

Out of scope for the pilot (say so in the docs): Windows on managed/domain
PCs with AppLocker or WDAC; Windows 10 (target Windows 11 22H2+, 10 only
by accident); 32-bit; a Windows `run_shell_command` sandbox; auto-update;
Microsoft Store.

## 7. End-to-end test plan: Windows 11 ARM VM

The existing rig is Tart (Apple Virtualization), which only runs macOS and
Linux guests, so the Windows persona can't be a Tart VM. `tools/vmgui.py`
itself is already product-neutral: it speaks VNC through `vncdo` and only
asks for a host and port from `~/.config/casper-vm/vnc/<vm>.json`. Two
helpers inside use macOS-only tools (`sips` to read the screenshot width);
fine, they run on the host Mac. What changes is the VM provider and the
way commands reach the guest.

**VM provider: UTM, headless QEMU (already in flight on the board).** Tart
only runs macOS and Linux guests, so the Windows persona needs another
provider; a separate task is building the UTM/QEMU VM (`windows-base`
snapshot, `-vnc`, OpenSSH). Nothing was installed for this document (checked:
no UTM, Parallels, VMware, QEMU, `utmctl`, `vmrun`, `prlctl`; only `tart`).
Notes on that choice: UTM/QEMU (`qemu-system-aarch64`, HVF) takes
`-vnc 127.0.0.1:N,password`, snapshots are `qemu-img snapshot` or clones, and
Windows 11 ARM needs the ARM64 ISO, the `virtio-win` drivers and a TPM
(`swtpm`, which UTM ships). It is free and fully scriptable but has slower
graphics. Fallbacks if graphics or drivers hurt: VMware Fusion (free for
personal use, built-in per-VM VNC, `vmrun` snapshots; check the licence for
unattended use) or Parallels (`prlctl`, paid).

Whichever provider is used, add a `tools/winvm.py` (or a `provider` field per
persona in `tools/scenarios.toml`) with the same verbs `scenario.py` already
uses for Tart: `clone/revert`, `start --vnc`, `stop`, `exec`, `put`/`get`
file copy, and record the VNC port where `vmgui.py` expects it.

**Guest setup (one-time, then snapshot `win-base`):**

1. Install Windows 11 ARM (no Microsoft account: `OOBE\BYPASSNRO`, local
   user `noah`, auto-logon so the desktop session exists after reboot).
2. Turn on **OpenSSH Server** (`Add-WindowsCapability -Online -Name
   OpenSSH.Server~~~~0.0.1.0`, set the service automatic, allow port 22,
   authorise the host's public key in `C:\ProgramData\ssh\administrators_authorized_keys`
   for an admin user or `%USERPROFILE%\.ssh\authorized_keys` otherwise).
   Default shell PowerShell: `New-ItemProperty HKLM:\SOFTWARE\OpenSSH -Name
   DefaultShell -Value powershell.exe`.
3. Disable what gets in the way of a clean run, in the *base* only: Windows
   Update, Defender cloud prompts (Defender stays on; add no exclusions
   except the download folder if SmartScreen blocks automation), the
   privacy/OOBE nags, screen lock and sleep, UAC unchanged.
4. Install Claude Code (needs Git for Windows and a native install) for the
   "riley" persona's role if a Windows persona ever plays the inviter; the
   first Windows persona is the **invitee** (noah), so only the Casper app is
   needed in the base; Chrome or Edge as the person's real browser.
5. Snapshot `win-base`; later checkpoints extend it as for the Macs
   (`noah-win-fresh`, `noah-win-installed`), and `scenario.py capture` stores
   the server rows with them.

**The one trap: session 0.** Commands run over SSH execute in a
non-interactive logon session with no desktop. A tray app, a
`MessageBox` or a folder dialog started from there are invisible and
unreachable from VNC, and the protocol handler would launch in the wrong
session. Tart's guest agent avoids this on the Mac by running in the user's
session. On Windows, launch GUI things in the user's session instead:
`schtasks /create /tn casper-run /tr "<cmd>" /sc once /st 00:00 /it /ru noah`
then `schtasks /run /tn casper-run` (the `/it` flag runs it interactively on
the logged-on desktop), or `PsExec -i 1` with `-s` off. The `Casper.exe
setup ...` commands, which are meant to be run by the person's *agent* (itself
in whatever session the person has), are fine in plain SSH as long as they
only print; the ones that show a dialog (account password, folder choice,
kit) must be run through the interactive-task helper. Build that helper into
the Windows `vm_exec` (`gui=True`) and use it for "install and start Casper".

**Steps for the invitee scenario on Windows** (mirror of the Mac steps in
`scenarios.toml`; each one a new `step_*` function):

1. Reset to `noah-win-fresh`; delete the persona's dev/test account (same
   devtools endpoint as the Macs).
2. Copy `Casper-windows-arm64-setup.exe` (and separately the amd64 one, run
   in the same VM through Windows' x64 emulation: both builds get tested in
   one VM) from the test stack's download URL with the browser or
   `Invoke-WebRequest`; run the installer silently.
3. Open the guide in Edge or Chrome with VNC clicks; the guide's
   "Download Casper" button path (SmartScreen "Keep" / "Run anyway") is
   exercised by clicking, once per release, not in every run; most runs use
   the scripted install to save time, like the Mac runs do.
4. Sign-up through the browser; the first-launch code flow connects the PC.
5. Folder choice, consent dialog, mirror to headless Riley (the same headless
   Mac-mini persona as now: a Windows-to-Mac mirror), first copy, recovery
   kit to a second virtual disk (attach a small VHDX as drive `E:` to
   test the removable-drive default), give back.
6. Reboot the VM and check the login item brought Casper back and presence
   resumed. Sign out and in, same.
7. `lost-mac` on Windows: capture, destroy the VM, restore on a fresh
   `win-base` clone from the kit; check a kit made on a Mac imports on
   Windows and the reverse.
8. Unit and package tests on the guest: copy the repo's `agent/` and run
   `go test ./...` natively on Windows (Go for windows/arm64 in the base).
   This also runs the secretstore tests.

**Looking at the screen.** Unchanged: `tools/vmgui.py shot|click|type|key`
over VNC. Notes: keyboard shortcuts differ (`ctrl-a` fine, add `win`,
`alt-tab`, `alt-f4`, `ctrl-alt-del` is intercepted by some VNC servers: avoid);
the screenshot scale cache uses `sips`, which works on any PNG. VNC servers
differ on shifted characters (the `SHIFTED` table exists because of this;
re-check it against Fusion or QEMU). Dialog text can also be read without
OCR through UI Automation over SSH in the interactive task (a tiny
PowerShell `System.Windows.Automation` dump), which is more reliable than
screenshots for assertions; screenshots stay the evidence in the report.

**Skill and reports.** `.claude/skills/e2e-run` gets a "Windows" section: the
same ground rules (test stack only, VNC only, nothing on the Principal's
screen, one VM at a time, 75-minute limit), the persona names, and where to
look for logs on Windows (`%LOCALAPPDATA%\Casper-test\command_log.txt`,
`...\syncthing\syncthing.log`, Event Viewer for crashes). VM memory: a
Windows 11 ARM guest wants 4 GB and 2 cores minimum, 8 GB comfortable;
`prepare_host` must free that, and a Windows VM and two Mac VMs at once is
not realistic on one Mac.

**Hands-off checks to run before any VM exists (from this Mac, now):**
`GOOS=windows go vet`, the cross-compile of both architectures, `file` on the
output, `go test -c` for the packages that have tests to confirm they link
for Windows (they compile; they run only in the guest).

## 8. Decisions and open questions

1. **Secrets on Windows**: DPAPI-sealed files (recommended) vs Credential
   Manager, and accepting that the user's own agent can read them. Needs the
   Principal's yes and a line in the risk model.
2. **Firewall**: pilot with no inbound listening (rely on dialling out and
   the relay), or ship an installer step with an elevated firewall rule.
   Recommend no inbound first; measure how many pairs then fall back to the
   relay.
3. **VM**: UTM/QEMU is being built; a Windows 11 ARM licence/ISO is needed,
   and about 80 GB of disk for base plus checkpoints.
4. **Signing route**: Trusted Signing vs an OV cloud-HSM certificate. Start
   the identity validation now; it can take weeks and is the long pole.
5. **Installer format**: Inno Setup `.exe` (recommended) vs MSIX.
6. **Host `platform` field** in the API and the wording rule "this computer".
7. **OneDrive-redirected known folders and cloud-only placeholders**: skip
   and report, or hydrate on demand.
8. **`run_shell_command` on Windows**: unavailable (recommended) or POSIX
   tooling assumed (Git Bash).
