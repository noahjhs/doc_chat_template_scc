# 👻 Casper

*your friendly ghost.*

Lets the deployed Doc Chat web app run a fixed, allowlisted set of local
commands — not arbitrary shell access — confined to the directory tree this
tool is located in.

## Run it

Double-click `Casper` (or run it from a terminal). It signs you in first,
then starts a Cloudflare Tunnel.

**First run**: it opens a browser tab to sign up or log in — a normal web
form, not a terminal prompt. Submitting it takes you straight to the chat
app in that same tab, and saves what it needs (`session.json`, next to the
executable) so every run after this one is silent — no prompt, no sign-in
tab, straight to the chat app.

No API key to copy-paste, no manual setup: once signed in, it takes you to
the chat app, already connected to this machine.

If you quit Casper and relaunch it while its chat tab is still open, that
tab reconnects itself to the new run in the background — no new tab opens
for it. Since Streamlit doesn't carry state across that kind of reconnect,
it starts as a fresh conversation rather than resuming the old one.

The web app domain it opens is baked in from `app_server.txt` (repo root) at
build time — just the bare domain, e.g. `my-app.streamlit.app`, no
`https://` and no path (`/chat` is appended automatically). The auth
service's domain is baked in the same way from `auth_server.txt` (not
optional — the build fails without it). To change either, update the
file(s) and rebuild (`build/build_macos.sh` or `build/build_windows.ps1`).

Pass `--agent-server` to open `localhost:8501` (Streamlit's default port)
instead of the baked-in domain — handy for testing without rebuilding or
editing `app_server.txt`. Pass `--agent-server HOST[:PORT]` to open a different
server instead (e.g. `--agent-server localhost:8502` or
`--agent-server some-other-host.example.com`).

## What it can do

Every action is confined to the directory tree this tool is located in — it
can never read, write, or navigate outside it, no matter what path a request
asks for (including via `..` or a symlink). Within that tree:

**Git** — `status`, `branch -a`, `log --oneline`

**Navigation** — `pwd`, `cd`, `ls`, `tree`

**Management** — `mkdir`, `touch`, `cp`, `mv`, `rm`, `rmdir`. `rm` only
deletes a single file; `rmdir` only removes an already-empty directory —
neither one ever deletes recursively.

**Viewing & Searching** — `cat`, `less`, `head`, `tail`, `grep`, `find`

It starts in whatever directory it's located in. `cd` moves it elsewhere
(still within the confined tree) and that sticks — later commands run in the
new directory until you `cd` again.

## Stopping it

Click "Sign out" in the web app's sidebar — this tells Casper to shut down
(and revokes the session, so it'll ask you to sign in again next time). You
can also just close the terminal window or quit the app directly. Either
way, this shuts down the tunnel too.

## Logs

Every command it's asked to run — accepted, rejected, or unauthorized — is
logged with a timestamp to `command_log.txt` next to the executable, as well
as printed to the console.
