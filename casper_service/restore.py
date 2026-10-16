"""Pure restore logic (docs/product/restore-design.md): reading times, which
version of each file was live at a moment, and whether a restore fits. The
daemon calls and the tools live in main.py."""

from datetime import datetime, timedelta, timezone

HISTORY_DAYS = 30
FIT_MARGIN = 256 << 20
REPLACE_MARGIN = 16 << 20  # a replace leaves the disk about as full as it was


def parse_time(text: str) -> datetime:
    """An RFC 3339 / ISO time -> aware datetime. A bare date means the end of
    that day; no offset means the Mac's own (the caller's `local`)."""
    t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if len(text.strip()) == 10:
        t = t.replace(hour=23, minute=59, second=59)
    return t


def when(text: str, local: timezone | None) -> datetime:
    t = parse_time(text)
    return t if t.tzinfo else t.replace(tzinfo=local)


def offset_of(versions: list[dict]) -> timezone | None:
    """The Mac's own UTC offset, read off its version times."""
    for v in versions:
        try:
            return parse_time(v["at"]).tzinfo
        except (ValueError, KeyError):
            continue
    return timezone.utc


def is_deleted(v: dict) -> bool:
    """Gone from the folder now (or recorded as deleted)."""
    return bool(v.get("deleted")) or v.get("exists_now") is False


def live_at(versions: list[dict], moment: datetime) -> dict | None:
    """The version of ONE file that was current at `moment`: written at or
    before it and replaced (archived) after it. None if the file didn't exist
    then, or is unchanged since (the current file is the answer)."""
    later = [v for v in versions if parse_time(v["at"]) > moment]
    if not later:
        return None
    v = min(later, key=lambda v: parse_time(v["at"]))
    try:
        if parse_time(v["modified"]) > moment:
            return None
    except (ValueError, KeyError):
        pass
    return v


def as_of(versions: list[dict], moment: datetime) -> dict[str, dict]:
    """name -> the version to bring back, for a folder as of `moment`."""
    by_name: dict[str, list[dict]] = {}
    for v in versions:
        by_name.setdefault(v["name"], []).append(v)
    out = {}
    for name, vs in by_name.items():
        v = live_at(vs, moment)
        if v is not None:
            out[name] = v
    return out


def oldest(versions: list[dict]) -> datetime | None:
    times = [parse_time(v["at"]) for v in versions]
    return min(times) if times else None


def fit(sizes: list[int], local_bytes: int, free: int) -> str:
    """Where a restore fits: "copy" (preferred), "replace" (only replace
    fits) or "none". A copy needs the restored bytes plus room for the
    largest file while it's decrypted, and leaves a margin; a replace needs
    only the transient room (the file it replaces is freed)."""
    largest = max(sizes, default=0)
    if free >= sum(sizes) + local_bytes + largest + FIT_MARGIN:
        return "copy"
    if free >= 2 * largest + REPLACE_MARGIN:
        return "replace"
    return "none"


def short_time(iso: str) -> str:
    try:
        return parse_time(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def pick_version(versions: list[dict], name: str, at: str, local: timezone | None) -> tuple[list[str], dict | None]:
    """Resolve what the person asked for. `name` is exact, or a part of a
    name that only one file matches. `at` is "" / "latest" (newest), an exact
    version time, or any other time (the version live then). Returns
    (candidate file names if it was ambiguous or unknown, the version)."""
    names = sorted({v["name"] for v in versions})
    if name in names:
        chosen = [name]
    else:
        chosen = [n for n in names if name.lower() in n.lower()]
    if len(chosen) != 1:
        return chosen, None
    mine = sorted((v for v in versions if v["name"] == chosen[0]), key=lambda v: parse_time(v["at"]), reverse=True)
    if at in ("", "latest"):
        return chosen, mine[0]
    exact = next((v for v in mine if v["at"] == at), None)
    if exact:
        return chosen, exact
    try:
        return chosen, live_at(mine, when(at, local))
    except ValueError:
        return chosen, None


def too_old(moment: datetime, now: datetime | None = None) -> bool:
    return moment < (now or datetime.now(timezone.utc)) - timedelta(days=HISTORY_DAYS)


def earliest(now: datetime | None = None) -> datetime:
    """The oldest moment history still reaches."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=HISTORY_DAYS)


def row_labels(versions: list[dict]) -> list[str]:
    """One distinct line per version for a list the person reads, e.g.
    'Budget.xlsx  (written 2026-10-14 09:12, 1.2 MB, deleted)'."""
    seen: dict[str, int] = {}
    out = []
    for v in versions:
        size = v["size"]
        human = f"{size / (1 << 20):.1f} MB" if size >= 1 << 20 else f"{max(size // 1024, 1)} KB" if size >= 1024 else f"{size} B"
        label = f"{v['name']}  (written {short_time(v['modified'])}, {human}{', deleted' if is_deleted(v) else ''})"
        seen[label] = seen.get(label, 0) + 1
        out.append(label if seen[label] == 1 else f"{label} #{seen[label]}")
    return out
