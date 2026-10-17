# What a friend's encrypted mirror shows

Status: measured 2026-10-17 (claims inventory finding 8, `docs/product/claims.md` C7).
Method: `tools/exposure_probe.py` builds a real mirror on the **test stack** (two
throwaway headless personas on the mini, real Syncthing in `receiveencrypted` mode, test
`casper_service`) and reads the holder's disk, the holder's Syncthing config and Casper's
catcher container from outside. The same was repeated locally with the integration harness
(`tests/test_mirror_integration.py`), which gave the same picture. The sample folder:
`notes.txt` (31 bytes, mode 600, mtime 2017), `taxes/2025/return.pdf` (3.0 MB), `taxes/w2.txt`
(10 bytes), an empty directory.

## What the holder can read

| | Holder sees | Verdict |
|---|---|---|
| File contents | ciphertext only | hidden |
| File and folder **names** | each path becomes one opaque name in a two-letter-sharded tree (`2.syncthing-enc/L3/PKRHT…`) | hidden |
| Directory structure | flattened: no parent/child relation is visible | hidden |
| Modification times | every encrypted file reads `mtime=1234567890` | hidden |
| Permissions | all files 644, all directories 755 (originals were 600/755) | hidden |
| **How many** files and directories | one entry per file and one empty directory node per directory (3 files + 3 directory nodes here) | **visible** |
| **Size** | file size + ~3.8 KB overhead for the 3 MB file; small files all look like ~1.4 KB (31 bytes → 1441, 10 bytes → 1427); total bytes in Syncthing's own counters | **visible** (to within ~4 KB for big files; small ones are padded together) |
| **When** things change | file/directory arrival times (`btime`, directory `mtime`), and the dates in version names (`…~20261017-130422`) | **visible** |
| That the folder exists, whose it is | the owner's device id and addresses; the folder id | **visible** |
| The folder's **name** | **was visible**: Syncthing's folder label read `riley: Documents` in the holder's own config and window | **fixed 2026-10-17**: the label is now `riley: encrypted mirror` (`mirroring.desired_state`); asserted against the real daemons in `tests/test_mirror_integration.py` |
| The folder's name, in Casper's notification to the holder | **was sent**: "{me}'s Documents is now mirrored on your computer" | **fixed**: `claims.say_friend_mirror_started` names no folder |

Casper's own catcher (the `catcher` container) holds the same kind of ciphertext, and only
for changes made while a mirror sleeps; on the test stack it held nothing after a normal
first copy. It never has a key.

Not measured here: traffic metadata a relay or network observer sees; whether a holder who
already knows a large public file could recognise it by size (possible by the table above).

## What the copy can honestly say

"Encrypted, so friends can never read them" is true of contents and names. It is not true of
"Names, folders and contents are sealed" before the label fix (folder names were not), and it
leaves out the visible rows above. A wording that matches the measurements:

> Your files and their names are encrypted before they leave your computer. A friend's
> computer can tell that a folder is there, roughly how big it is, how many files it has and
> when it changes. It can't read what's in it or what anything is called.

Whether to put this in the invite, the site and the guide is the Principal's call (board:
"Needs you"); nothing in the site copy was changed.
