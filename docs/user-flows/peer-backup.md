# Peer backup (v1)

The v1 "releasable" bar (docs/product/README.md): an agent, through Casper's
MCP server, backs up a folder from one person's machine to a friend's,
encrypted so the friend can't read it, with the friend approving it, then
restores it. Scenario: [docs/product/scenarios/peer-backup.md](../product/scenarios/peer-backup.md).

## Preconditions

- Two real Macs, each running a dev build of Casper (`DEPLOY_ENV=dev
  ./build/build_go_macos.sh`). **Sam** is paired on one (the mini) and
  **Riley** on the other (a laptop). Use fresh accounts.
- Optional: both accounts linked to Telegram (`harness profile
  telegram-link`) to answer approvals by tapping. Without it, answer them
  with `harness approvals list`/`respond` as that account.
- Riley's agent: Claude Code with Casper added as an MCP server
  (`harness agent token create <name>` prints the exact `claude mcp add`
  line).

## Steps

- [x] 1. Riley: `harness friends add <sam>`. Sam accepts (approval kind
      `friend_request`).
      **Expected:** both see each other in `harness friends list`.
- [x] 2. Sam: `harness offerings publish --host <mini> --max-gb 5 --tier ask`.
- [x] 3. Riley's agent: `list_offerings`, then `request_access`. Sam grants
      it (kind `access_request`).
      **Expected:** Sam's approval reads "<riley> is asking for 1 GB of
      backup space on <mini>". Riley's `list_hosts` now shows
      `<sam>/<mini>` with role `backup_peer`.
- [x] 4. Riley's agent: `backup_push(source_host=<laptop>,
      path=<folder>, dest_host=<sam>/<mini>)`.
      **Expected:** returns at once, saying it's waiting for Sam. Sam's
      approval shows a size and quota, **not the folder name**.
- [x] 5. Sam approves (kind `backup_write`).
      **Expected:** `backup_status` reaches `complete`. Riley is notified.
- [x] 6. On the mini, look under
      `~/Library/Application Support/Casper-dev/backups/<sam>/peer/<riley>/`.
      **Expected:** only `chunk-*` files, `manifest.json` (sizes and hashes,
      no names) and `complete`. Grepping for the folder name or any file
      name or content finds nothing.
- [x] 7. Riley's agent: `backup_restore(backup_id, dest_host=<laptop>)`.
      **Expected:** a new `~/Casper Restores/<folder>-<timestamp>`. Every
      file is byte-identical to the original.
- [x] 8. Sam: `harness grants revoke <id>`.
      **Expected:** Riley's next `backup_push` there is refused. Riley's
      `backup_list` still shows the stored backup (7-day grace).

## Pass/fail

**2026-09-28 dev run: pass, all 8 steps confirmed live.** Sam was on the
Mac mini and Riley on the MacBook, both real dev daemons built and
notarized that day. The scenario was driven through the real public
`https://dev-auth.casperagent.dev/mcp` with raw MCP JSON-RPC (the same
`tools/call` requests an MCP client sends), and approvals were answered
through the API.

- A 6 MB folder (a random 6 MB file plus a text note) went out as 2 chunks
  and completed about 3 seconds after approval.
- The mini's disk held only chunks and a name-free manifest; grepping for
  the folder name, the file names and the note's content found nothing.
- The restore matched: SHA-256 of both files identical to the originals.
- After Sam revoked the grant, Riley's push was refused while the stored
  backup stayed listed.
- Passphrase key export worked for the owner, and Sam got a 404 trying to
  export Riley's key.
- Revoking the agent token turned `/mcp` into a 401.

Two parts are **not yet confirmed live**:
- Claude Code itself as the client. The requests were what it sends, but
  it hasn't been connected for real.
- Answering the new approval kinds with a Telegram tap.

## Known issues

- **iCloud Keychain sync isn't available to this build** (found 2026-09-28,
  expected). Both machines reported `stored: local` when creating keys, so
  the synchronizable item was refused and the local fallback was used, as
  planned. Until that changes, losing a machine loses the keys for backups
  it made unless they were exported:
  `harness backup export-key --host <label> --out <file>`. Nothing prompts
  people to do that yet.
- **Host names are the machine's own hostname** (e.g. `Noahs-Mac-mini`),
  so a shared host appears as `<owner>/Noahs-Mac-mini`. That's accurate,
  but it isn't friendly; `harness hosts rename` fixes it per owner.
