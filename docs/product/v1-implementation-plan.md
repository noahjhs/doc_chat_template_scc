# v1 implementation plan: peer backup

Drafted 2026-09-28. Builds [scenarios/peer-backup.md](scenarios/peer-backup.md).

**Status (2026-09-28):** all phases built; the scenario passed live on dev
with real daemons on two machines ([docs/user-flows/peer-backup.md](../user-flows/peer-backup.md)).
Still to confirm live: Claude Code itself as the client, and Telegram taps
on the new approval kinds. The Keychain spike landed on the planned
fallback: iCloud sync was refused, so keys are stored locally and the
passphrase export is the recovery path.
Each phase ends in something testable. Phases run in order; the Principal
signs off on the plan once, not phase by phase.

## Principles

- **The daemon stays authoritative.** Every backup write, read and delete
  on a peer's host is decided by *that host's* daemon against grants it
  fetched itself, the same way `run_shell_command` is decided against its
  cached policy layers. `casper_service` orchestrates and never decides.
- **Ciphertext only, off the source host.** Encryption and decryption
  happen only in the backup owner's own daemon.
- **Additive.** The OpenAI conversation loop stays in place until v1 ships
  and is removed afterwards in a separate cleanup, so nothing working
  breaks mid-build.
- **ReBAC-shaped from day one,** without adopting OpenFGA or SpiceDB yet: a
  single tuple table that we can migrate later.

## Phase 0: foundations

1. **Tunnel message size.** `coder/websocket` defaults to a 32 KB read
   limit per message, and neither `relay/internal/registry/conn.go` nor
   `agent/internal/tunnel/tunnel.go` raises it. Any tunneled request or
   response over 32 KB fails today, including large command output.
   - Set an explicit limit (16 MB) on both ends.
   - Add a test that round-trips a 5 MB body.
   - Backup chunks will be 4 MB, about 5.4 MB after base64.
2. **Generic notifications.** Extract `_send_approval_telegram` into
   `notify(user_id, text, buttons=None)`, so the same path can send outcome
   notifications to requesters.
3. **Generic approvals.** Extend `pending_approvals` with:
   - `kind`: `conversation` (existing), `access_request` or `backup_write`;
   - `payload` (JSON);
   - `requester_user_id`, since the approver (`user_id`) is no longer
     always the person who asked.

   `decide`, the Telegram webhook and `harness approvals` dispatch on
   `kind`.

## Phase 1: MCP plumbing, single host

The "prove the pipes" milestone.

- Mount an MCP server at `/mcp` in `casper_service`, using the official
  Python `mcp` SDK over the streamable HTTP transport.
- **Agent tokens:** a new `agent_tokens` table (user, name, hash, created,
  revoked), created with `harness agent token create <name>`. A token maps
  to its principal. This is the "on behalf of" link, and each token is its
  own subject for later scoping and audit.
- Tools:
  - `list_hosts`
  - `run_shell_command` on the caller's own hosts, reusing
    `_dispatch_shell_command`. An ask-tier call returns immediately with
    "waiting for approval", and the outcome is notified.
- **Milestone:** Claude Code, connected with an agent token, runs `uptime`
  on `casper-mini` through MCP. An ask-tier command pauses and is approved
  in Telegram.

## Phase 2: trust framework data

- **Tables:**
  - `relation_tuples(subject_type, subject_id, relation, object_type,
    object_id, attrs, created_at, revoked_at)`, holding `friend` and
    `backup-peer` (whose attrs include the quota and write tier);
  - `friend_requests`;
  - `offerings(owner, host, kind='backup_space', audience='friends',
    max_quota_bytes, write_tier)`;
  - `access_requests(offering, requester, quota_bytes, status)`.
- **Endpoints:** friend request, accept, list and remove; offering
  publish, list and withdraw; access request; grant revocation.
  - An access request becomes an `access_request` approval for the owner,
    delivered through Telegram.
  - Approving it writes the `backup-peer` tuple.
- **Harness:** `harness friends …`, `harness offerings …`,
  `harness access …`.
- **MCP tools:** `list_offerings` (what my friends offer) and
  `request_access`. The agent can do Riley's browsing and requesting.
  `list_hosts` gains the hosts the caller holds a grant on, labelled
  e.g. `sam-mini (Sam)`.

## Phase 3: daemon backup actions (Go)

- **Keys.** The daemon generates, per paired identity:
  - an **age X25519** key for encryption;
  - an **ed25519** key for signing.

  Both are stored in the macOS Keychain. The public signing key is
  registered with `casper_service`. `harness`/the daemon also offer a
  passphrase-wrapped key export.
  - *Spike first:* synchronizable (iCloud) Keychain items need Security
    framework calls through cgo and a keychain-access-groups entitlement.
    If that fights us, v1 ships with the local login keychain and a
    **mandatory** passphrase export, and syncing follows.
- **Format.** tar, then `filippo.io/age` (authenticated, streaming, and
  hides names and structure), split into 4 MB chunks. There is a manifest
  (backup ID, chunk count, sizes, and chunk hashes) signed with ed25519.
- **Grants.** The daemon fetches `GET /hosts/grants`, gated by device
  token like `GET /hosts/policy-layers`, and can be told to refresh them.
  Each grant is: grantee, grantee's public signing key, quota, write tier,
  and `revoked_at`.
- **Actions:**
  - *On the source host (the owner's own identity):* `backup_prepare(path)`
    stages encrypted chunks and returns the ID, size and chunk count;
    `backup_read_chunk`; `backup_restore_chunk`; `backup_unpack(dest)`
    decrypts into `~/Casper Restores/<name>-<date>` and never overwrites.
  - *On the peer host:* `backup_write_chunk(grantee, backup_id, index,
    total_bytes, approved)`, re-matched fresh on **every** chunk against the
    grant's tier, the quota and the manifest signature. Then `backup_list`,
    `backup_get_chunk` and `backup_delete`, each limited to the grantee's
    own blobs and allowed during the grace period after revocation.
  - Blobs live under the daemon's application-support directory, per
    identity and per grantee, and are never executed.
- Go unit tests for each action.

## Phase 4: orchestration and MCP tools

- `backups` table: ID, owner, source host, destination host, status, size,
  timestamps.
- `backup_push` works like this:
  1. Fail fast if either host is offline.
  2. `backup_prepare` on the source host.
  3. Probe the destination daemon for its verdict.
     - **deny:** fail and notify.
     - **ask:** create a `backup_write` approval for Sam, and return
       "waiting for Sam" to the agent.
     - **allow:** proceed.
  4. Once approved or allowed, stream the chunks in a background worker,
     source → destination, re-sending `approved`.
  5. Notify Riley on completion, denial or failure.
- **MCP tools:** `backup_push`, `backup_status`, `backup_list`,
  `backup_restore`, `backup_delete`.
- **Revocation:** writes fail immediately. Restore and delete stay allowed
  for 7 days; after that, the peer daemon purges the blobs on its next
  grant refresh. Riley is notified.
- Extend `tests/fake_daemon.py` and `tests/` to cover the full flow with no
  real daemons.

## Phase 5: real verification

- `docs/user-flows/peer-backup.md`, walking the scenario for real:
  - two accounts: Sam on `casper-mini`, Riley on the laptop;
  - Claude Code as Riley's agent;
  - a real Telegram approval;
  - a real restore;
  - checking that the files on `casper-mini` are unreadable.
- Deploy to dev (following the standing auto-deploy rule).

## Alpha limits

- A 2 GB cap per backup, with transfers passing through relay and
  `casper_service`.
- A whole folder each time: no incremental backups, deduplication or
  schedules.
- macOS only.

## Risks

- **Keychain syncing entitlement:** see Phase 3's spike; there's a fallback.
- **MCP client support:** Claude Code accepts a bearer header for HTTP MCP
  servers. Claude Desktop and claude.ai connectors expect OAuth, which v1
  doesn't provide.
- **Bandwidth through the mini:** acceptable for a hand-picked alpha;
  direct transfer is deferred.

## After v1

- Remove `conversations.py`'s OpenAI loop and the `harness chat` surface.
- OAuth for MCP.
- Approver delegation, including to agents, as the happy path.
- Offers with duties, conditions on offerings, replication to several
  peers, and splitting keys among friends.
