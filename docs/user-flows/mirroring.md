# Mirroring with history (v1)

A person's agent sets up mirroring of a folder to a friend's Mac, the person
confirms in Casper's own dialog, and the folder is then kept continuously,
encrypted, with history. See
[docs/product/scenarios/mirroring.md](../product/scenarios/mirroring.md).

## Automated coverage

- `agent/internal/mirror`: Go tests against real Syncthing. They cover the
  encrypted mirror; a sleeping mirror flagged as unprotected; a catcher
  holding only the gap and emptying when the mirror wakes; versions
  decrypted and restored; pruning; a replacement-Mac restore; orphan
  cleanup.
- `tests/test_mirror_integration.py` runs the whole design in about 30 s
  with a real `casper_service` and four real daemons and Syncthings, driven
  through MCP:
  - an agent's attempt to confirm is refused, and the person's confirmation
    works;
  - the peers hold only ciphertext;
  - an old version comes back;
  - while the mirror sleeps, the catcher holds the gap;
  - **a lost Mac is rebuilt from the mirror plus the catcher's gap**,
    including the change the mirror never saw.

## Live run: 2026-10-01, dev, two real Macs. Pass.

- **Sam:** a fresh Claude Code on the Mac mini (always on).
- **Riley:** a fresh Claude Code on the MacBook, with Casper uninstalled
  first; the agent installed the new build (Syncthing bundled, notarized).

| Step | Result |
|---|---|
| Sam: "go" → account, pairing, restart → offering | The agent sized the offer to the disk (20 GB per friend), and **suggested catcher space because the Mac is always on**. It asked nothing in return. |
| Sam: invites | Two ready-to-send messages (mirror and catcher) |
| Riley: given both invites | Installed Casper from the public download, created the account, paired, restarted, previewed and redeemed both invites. It noted on its own that **the catcher is on the same machine as the mirror**, so there's no fallback. It **mentioned offering space back, without pushing**. |
| Riley: mirror `~/casper-live-test` | Preview, then "a Casper dialog is open — I can't click it for you". **The Principal clicked Allow; mirroring was configured about 20 s later.** |
| On the mini | 2 files, 3 MB, names and contents encrypted (`R.syncthing-enc/CS/…`); searching for names and contents found nothing |
| "I regret that edit" | `list_versions` → `restore_version`. The old recipe was restored as a new copy; the current file was untouched. |
| Mini quits Casper; Riley adds a file | "Not completely": 1 unprotected change, the mini offline since 21:42, mirror and catcher on the same machine. It suggested a second mirror. |
| Mini back | "Protected again": 3 files on the mini, plus 1 earlier version |
| Recovery kit | The Principal typed the passphrase into the dialogs; the kit covers the mirrored folder |
| Stop (test cleanup) | Stopped; Sam's copy is deleted after 7 days |

The disaster restore (a replacement Mac) was not repeated live; it passes
in the integration test.

## Found during the run

- **A missing Dockerfile entry crash-looped dev's auth service** for about
  two minutes after deploying (`mirroring.py` wasn't in the image's COPY
  list). Fixed, plus a test that fails if any module is missing from that
  list.
- **Claude Code hit the subscription usage limit** mid-run, on the mini's
  token. A retry a few minutes later worked.

## Known issues

- ~~**A catcher on the same machine as the mirror adds nothing.**~~ **Fixed
  2026-10-02:** `mirror_folder` refuses it, and the skills explain why.
- ~~**Protection status gives counts, not names.**~~ **Fixed 2026-10-02:**
  up to 10 unprotected files are named. The names are fetched live from the
  owner's own Mac when status is requested, and never stored on
  `casper_service`.
- ~~**No relay yet.**~~ **Fixed 2026-10-02:** Casper's own private relay
  (`strelaysrv` on the mini, not joined to the public pool) is published
  through Tailscale Funnel on port 10000. Every Mac and the catcher listen
  on it and advertise it. Verified with two Macs given only the relay
  address: they connected as `relay-client` and mirrored through it.
  *Still to confirm:* the relay's public DNS name didn't resolve outside
  Tailscale yet when checked (a Funnel DNS publication delay); recheck it
  from a non-tailnet network.
- ~~**The consent endpoint accepts this Mac's device token.**~~ **Fixed
  2026-10-02:** daemon sessions moved from `session.json` into the login
  Keychain, with a one-time migration that deletes the file (verified on the
  mini). Other programs reading the item get macOS's own access prompt.
- **Prod's Compose file is a locally edited copy** (it renames containers
  for prod). Adding the catcher, the relay and the Funnel there needs doing
  by hand at promotion.
