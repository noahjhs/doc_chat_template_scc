# Risk model

Abuse and failure categories to keep in mind as we build. Started
2026-09-28 during peer-backup design. Add rows as new workflows introduce
new risks.

## Private backups vs. illegal content

For people to back up personal data, backups must be **unreadable by the
backup host**. That conflicts with any guarantee that stored content is
legal. The top concern is CSAM; piracy is a lesser concern.

Decision (2026-09-28): **remove the distribution path** rather than screen
content. Most of the harm comes from distribution, not storage.

1. **Only the owner can decrypt.** There are no sharing links, no read
   grants on backup contents, and no public or stranger-readable blobs.
2. **Names and directory structure are encrypted**, not just file contents.
3. **Encryption happens on the source daemon.** The relay,
   `casper_service` and the backup host only ever handle ciphertext.
4. **Every blob is signed with the uploader's account key.** Responsibility
   traces to the uploader. The host can always delete blobs and revoke
   access.
5. **Backup peering requires an existing relationship.** No strangers.
6. **Quotas and rate limits apply.**

**Deferred to a later release:** content scanning and hash matching (e.g.
PhotoDNA, NCMEC or Thorn Safer hash lists) before encryption. These tools
catch only *known* material. They also can't stop someone who encrypts
content with their own key before handing it to the daemon, whether the
daemon is patched or genuine and attested, because the scanner then sees
only noise.

**Deferred until before a wider release:** legal review. This includes US
reporting duties on actual knowledge (18 U.S.C. § 2258A), the EU and UK
regimes, and the legal position of a host owner storing ciphertext they
can't read. v1 is an alpha for hand-picked participants only.

## Categories

| Risk | Who is harmed | Main mitigation |
|---|---|---|
| CSAM / illegal content storage | Victims, host owner, us | The six points above; legal review before wider release |
| Non-consensual intimate imagery | Victims | Same; StopNCII hash matching if anything ever becomes shareable |
| Copyright / piracy | Rights holders | No distribution path; a registered DMCA agent if anything becomes shareable |
| Malware placed on a host | Host owner | Blobs are never executed; stored outside policy-executable paths |
| Resource abuse (disk, bandwidth) | Host owner | Quotas, rate limits, usage visible to the owner |
| Host misbehaves toward backups (deletes, withholds, tampers) | Backup owner | Replicate to several peers; authenticated encryption detects tampering |
| Metadata leakage (sizes, timing, who backs up with whom) | Backup owner | Encrypted names, chunking and padding; some leakage accepted for v1 |
| Access misuse beyond what was granted | Host owner | Daemon-authoritative policy, ask-tier, instant revocation |
| Harassment or coercion through the social graph | Users | Blocking, one-sided revocation, no discovery of strangers in v1 |
| Compromised account or agent | Everyone connected to it | Scoped grants, ask-tier on sensitive actions, key rotation, audit log |
| Lost encryption key | Backup owner | v1: synchronizable macOS Keychain item plus passphrase export; later, splitting the key among friends (see [scenarios/peer-backup.md](scenarios/peer-backup.md)) |
