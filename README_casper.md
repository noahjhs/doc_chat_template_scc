# 👻 Casper

*your friendly ghost.*

A small background app (the Go agent, in `agent/`) that lets your account's
signed-in web session act on your own machine: a fixed, allowlisted set of
local commands confined to a workspace of directories you choose, a
policy-governed shell command tool (rules you author yourself, see the
`harness` package), and file transfer to/from another connected machine or
your own private server storage.

## Run it

Move it wherever you'd like its workspace to live, then open it once.
Casper appears as a small icon in your status bar (look for 👻) and quietly
waits there — consider adding it to your Login Items so it's always ready.

It doesn't open a sign-in tab itself. Instead, sign in from the website
(wherever `AUTH_SERVICE_DOMAIN`/`APP_SUBDOMAIN_DOMAIN` point this build at)
— on success, the site hands a `casper://pair` URL to this app, which
completes pairing automatically, no copy-pasting a token or key. From then
on, add directories to its workspace from the site's own sidebar ("Add
directory" opens a native folder picker on this machine).

## What it can do

**Addressable directories** — whichever folders you've added via the web
app's own "Add directory" control (sidebar's Workspace section). Every
action below is confined to those trees; it can never read, write, or
navigate outside them, no matter what path a request asks for (including
via `..` or a symlink).

**Local commands** (a fixed allowlist, always available):
- Git — `status`, `branch -a`, `log --oneline`
- Navigation — `pwd`, `cd`, `ls`, `tree`, `list_directories`
- Management — `mkdir`, `touch`, `cp`, `mv`, `rm`, `rmdir` (`rm` only
  deletes a single file; `rmdir` only removes an already-empty directory —
  neither one ever deletes recursively)
- Viewing & Searching — `cat`, `less`, `head`, `tail`, `grep`, `find`

**Shell commands** — arbitrary binaries/arguments, but only ever run if
they match a rule in one of the Policy Layers attached to this host (see
the `harness` package's `policy apply` for authoring these); an
unattached-by-default host runs nothing at all. Each rule's tier decides
what happens next: `allow` runs immediately, `ask` pauses for your
explicit approval (in the client driving the conversation, or as a native
dialog here if this machine is your currently "attended" host), `deny`
always rejects it.

**File transfer** — moving a file to/from another connected machine, or
to/from your own private server storage (capped at 1GB, independent of any
machine).

## Stopping it

Sign out from the web app (this pushes a shutdown to every attached
daemon and revokes the session), or use "Quit" from this app's own tray
menu directly. Either way, this shuts down its tunnel too. The tray menu
also has "Pause"/"Resume" (stop responding to commands without fully
quitting) and "Restart" (relaunches from wherever it's currently
installed).

## Logs

Every command it's asked to run — accepted, rejected, or unauthorized — is
logged with a timestamp to `command_log.txt` next to the executable (or
alongside its config directory, depending on how it was launched), as well
as printed to the console.

## Building it

`build/build_go_macos.sh` (repo root) builds, code-signs, and notarizes a
macOS `.app`, then zips it. `DEPLOY_ENV=dev` picks the dev deployment's
baked-in `auth_server.dev.txt`/`relay_server.dev.txt`/`app_server.dev.txt`
domain files instead of the plain (prod) ones — see the script's own
comments for the full dev/prod domain-targeting story. There is currently
no equivalent Windows build script, even though the agent's own source has
Windows-specific files (`agent/internal/config/workspace_windows.go` etc.)
— Windows support is a known, tracked gap, not yet built.
