# Spike: Syncthing as the mirroring engine

**2026-10-01. Result: Syncthing works for mirroring with history.** It
passed every test, with one small gap that Casper would wrap (restoring old
versions).

Context: backup as a separate app isn't viable; backup is plumbing. What
Casper can bring to market is **asynchronous remote mirroring with
history**: one or two peers keep an encrypted, versioned copy of your
folders, updated whenever a file changes. Rather than build the
replication engine, we tested whether Syncthing's *untrusted (encrypted)
devices* feature already is one.

## Setup

- **Syncthing v2.1.5** on the MacBook (trusted, owns the data) and the Mac
  mini (the friend's machine, untrusted).
- Each instance had its own home directory and ports, isolated from
  everything else.
- Global discovery, local discovery, relays, NAT traversal and usage
  reporting were **all off**. The peers knew each other only by Tailscale
  address, as Casper would configure them.
- **Everything was driven headlessly through the REST API.**
- The laptop's folder was shared with the mini **with an encryption
  password**. The mini's copy was type **`receiveencrypted`**, with
  **staggered versioning, 30 days**.

## Results

| Test | Result |
|---|---|
| Peer can't read anything | Names and paths are encrypted (`P.syncthing-enc/HP/9R6J16…`). Searching every byte on the peer for file names, folder names and contents found nothing. Visible: file count, approximate sizes, version timestamps (the metadata the risk model accepts for v1). |
| Initial sync | 6 MB mirrored before I could measure (seconds), over direct TLS 1.3 |
| A change reaches the peer | **≈2.2 s** for a new file, an edit, or 1 MB changed inside a 6 MB file. About 1 s of that is the deliberate settle time. Only changed blocks travel. |
| Peer offline, then back | Caught up on add + edit + delete **2.1 s** after restart |
| History on the encrypted side | Works. Overwritten and deleted files are kept in `.stversions`, timestamped, **still encrypted** |
| **Lost laptop** | A brand-new device, given only the folder password and the peer, rebuilt the whole folder from the peer's encrypted copy in **3 s**, identical (`diff -r`) |
| **Accident** (old or deleted version) | Every version decrypted with the password: both earlier edits, the 6 MB file from before the overwrite (confirmed by content), and the deleted file. **Gap:** `syncthing decrypt` silently skips files in `.stversions` because of the `~timestamp` suffix; stripping it into a per-timestamp tree first works. Casper would own this "restore a version" step. |

## What this means for the design

- **The engine is solved:** change detection, block-level transfer,
  catching up after being offline, encrypted peers, history and recovery.
  Casper's daemon would bundle Syncthing (MPL-2.0; a single ~25 MB
  binary), run it as a child process with its own home, and configure it
  only through the REST API. People never see it.
- **Casper's value is everything Syncthing makes people do by hand:**
  - exchanging device IDs → our invites and friends;
  - folder IDs, passwords and per-device encryption settings → set by
    Casper, with the password kept in the Keychain and the recovery kit;
  - who mirrors whom, quotas, revoking, the ledger, consent → the trust
    framework;
  - restoring versions → a Casper action;
  - setup → agent onboarding.
- **Data path:** direct between peers, never through `casper_service`.
  Outside a shared Tailscale network, peers need discovery and a relay
  fallback for NAT. We'd run Syncthing's own open-source discovery and
  relay servers (`stdiscosrv`, `strelaysrv`) ourselves rather than use
  the public ones, for sovereignty.
- **Two peers** is just a second untrusted device on the same folder.
- **The folder password becomes the key.** Losing it means losing the
  mirror, so the recovery kit protects it, as it did the age keys.

## Not tested yet

- Peers on different networks, without Tailscale (discovery and relays).
- Large folders and real-world churn (thousands of files, photo
  libraries).
- How staggered versioning thins history over the full 30 days, and
  whether a simpler "keep everything for N days" is better for people.
- Quotas: Syncthing has no per-peer quota, so Casper would have to
  enforce it, for example by checking folder size before sharing and
  monitoring after.
