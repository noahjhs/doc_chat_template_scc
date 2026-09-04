# Doc Chat Control API

Lets the deployed Doc Chat web app run a few read-only `git` commands
against a repository on your own machine.

## Run it

Double-click `control_api` (or run it from a terminal). It starts a
Cloudflare Tunnel and prints a public URL like
`https://random-words.trycloudflare.com`, then opens the web app in a new
browser tab (if a URL was baked in — see below).

Its API key is baked in at build time from `LOCAL_AGENT_API_KEY` in
`.streamlit/secrets.toml` — the same value the web app itself reads, so a
freshly built/downloaded copy already matches without any copy-pasting. The
web app URL it opens on launch is baked in from `app_url.txt` (repo root) at
build time — leave that file empty to skip the auto-open. To change either,
update the file/secret and rebuild (`build/build_macos.sh` or
`build/build_windows.ps1`).

## Connect the web app

In the web app's sidebar, under "Local agent", enter the
`https://...trycloudflare.com` URL printed at startup. The API key field is
pre-filled from secrets.toml already.

The tunnel URL changes every time you restart the app — you'll need to
re-enter it each session.

## What it can do

Only a handful of read-only commands, plus changing directory — nothing else:

- `git status`
- `git branch -a`
- `git log --oneline`
- `ls -la`
- `cd` to another directory

It starts in whatever directory you launch it from, or wherever the
`.app`/executable is located. `cd` moves it elsewhere and that sticks — later
git commands run in the new directory until you `cd` again.

## Stopping it

Close the terminal window, or quit the app. This also shuts down the tunnel.

## Logs

Every command it's asked to run — accepted, rejected, or unauthorized — is
logged with a timestamp to `command_log.txt` next to the executable, as well
as printed to the console.
