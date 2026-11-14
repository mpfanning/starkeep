#!/usr/bin/env python3
"""Shared playlist matching: Spotify (artist,title) -> Navidrome song id.

Strategy:
  1. Progressive Navidrome search: full artist+title -> first artist+title
     -> punct-stripped first artist -> title-only (each x title variants)
  2. Strong normalization: NFKD fold, strip ALL non-alphanumeric
  3. 5-layer ladder per candidate set: exact / feat / flex / fuzzy / first
  4. Local-file-index fallback (catches Navidrome tokenization gaps, e.g.
     "Breakup" vs "Break Up", or an artist credited differently on Qobuz).
"""
import difflib
import json
import os
import re
import time
import unicodedata

from starkeep.subsonic import _strip_feat

LIB_ROOT = "/Volumes/Media/library/music"
_AUDIO_EXTS = {"flac", "m4a", "mp3", "ogg", "opus", "aac", "webm"}
_LOCAL_CACHE = "/tmp/local_music_index.json"


def _strong_norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.casefold()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def first_artist(a: str) -> str:
    return (a or "").split(",")[0].strip()


def _artist_key(a: str) -> str:
    t = _strong_norm(a)
    t = re.sub(r"\b(?:feat|ft|featuring|with)\b", " ", t)
    t = re.sub(r"\band\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.startswith("the "):
        t = t[4:].strip()
    return t


def _artist_set(a: str) -> set:
    out = set()
    components = [c.strip() for c in (a or "").split(",") if c.strip()]
    for base in [a, *components]:
        k = _artist_key(base)
        if k:
            out.add(k)
            out.add(k.replace(" ", ""))
    out.discard("")
    return out


_VERSION_PATTERNS = [
    r"\s*[-–—]\s*(\d{4}\s+)?remaster\w*\b.*$",
    r"\s*[-–—]\s*(\d{4}\s+)?version\b.*$",
    r"\s*[-–—]\s*mono\b.*$",
    r"\s*[-–—]\s*(\d{4}\s+)?live\b.*$",
    r"\s*[(]\s*(\d{4}\s+)?remaster\w*\b[^)]*\)$",
    r"\s*[(]\s*live\b[^)]*\)$",
    r"\s*[(]\s*with\s+[^)]*\)$",
    r"\s+(?:19|20)\d{2}\s*$",
    r"\s*[(]\s*from\s+[^)]*soundtrack[^)]*\)$",
    r"\s*[-–—]\s*single (?:version|edit)\b.*$",
    r"\s*[-–—]\s*radio (?:edit|mix)\b.*$",
    r"\s*[-–—]\s*\d*\s*\"?\s*edit\b.*$",
    r"\s*[-–—]\s*from the .*$",
    r"\s*[-–—]\s*from \"[^\"]*\".*$",
    r"\s*[-–—]\s*from \u201c[^\u201d]*\u201d.*$",
    r"\s*[-–—]\s*original (?:motion picture )?soundtrack\b.*$",
    r"\s*[-–—]\s*soundtrack\b.*$",
    r"\s*[-–—]\s*the original\b.*$",
    r"\s*[(]\s*from the .*\)$",
]

_TAIL_KEYWORDS = re.compile(
    r"remaster|version|soundtrack|edit|remix|\bmix\b|original|anniversary|live\b|without\b|from\b|mono\b",
    re.I,
)


def strip_versions(t: str) -> str:
    s = t or ""
    prev = None
    while prev != s:
        prev = s
        for p in _VERSION_PATTERNS:
            s = re.sub(p, "", s, flags=re.I)
    s = s.strip(" -–—")
    if " - " in s:
        base, _sep, tail = s.rpartition(" - ")
        if _TAIL_KEYWORDS.search(tail):
            s = base.strip(" -–—")
    return s.strip()


def flex_title(t: str) -> str:
    return _strong_norm(strip_versions(_strip_feat(t)))


def _artist_match(disk_artist: str, want_keys: set) -> bool:
    k = _artist_key(disk_artist)
    if not k:
        return False
    cands = {k, k.replace(" ", "")}
    if k.startswith("the "):
        cands.add(k[4:])
        cands.add(k[4:].replace(" ", ""))
    cands.discard("")
    if cands & want_keys:
        return True
    for want in want_keys:
        for cand in cands:
            min_len = 6 if " " not in want else 8
            if len(want) >= min_len and len(cand) >= min_len:
                if cand.startswith(want) or want.startswith(cand):
                    return True
    return False


def _match_candidates(cand: list, artist: str, title: str):
    want_t = _strong_norm(title)
    want_t_feat = _strong_norm(_strip_feat(title))
    want_t_flex = flex_title(title)
    av = _artist_set(artist)
    av_feat = _artist_set(_strip_feat(artist))

    for s in cand:
        if _strong_norm(s.get("artist")) in av and _strong_norm(s.get("title")) == want_t:
            return s["id"], "exact"
    for s in cand:
        if _artist_match(s.get("artist", ""), av_feat) and _strong_norm(_strip_feat(s.get("title", ""))) == want_t_feat:
            return s["id"], "feat"
    for s in cand:
        if _artist_match(s.get("artist", ""), av) and flex_title(s.get("title", "")) == want_t_flex:
            return s["id"], "flex"
    best, best_r = None, 0.0
    for s in cand:
        if not _artist_match(s.get("artist", ""), av):
            continue
        r = difflib.SequenceMatcher(None, flex_title(s.get("title", "")), want_t_flex).ratio()
        if r > best_r:
            best, best_r = s, r
    if best is not None and best_r >= 0.85:
        return best["id"], f"fuzzy{best_r:.2f}"
    fa = _artist_key(first_artist(artist))
    for s in cand:
        dk = _artist_key(s.get("artist", ""))
        if dk == fa or dk.replace(" ", "") == fa.replace(" ", ""):
            if _strong_norm(s.get("title")) == want_t:
                return s["id"], "first"
    return None, None


# ---- local file index fallback -------------------------------------------

def _load_local_index(max_age: int = 3600):
    if os.path.exists(_LOCAL_CACHE) and time.time() - os.path.getmtime(_LOCAL_CACHE) < max_age:
        with open(_LOCAL_CACHE) as fh:
            return json.load(fh)
    from mutagen import File as M
    entries = []
    for root, _dirs, files in os.walk(LIB_ROOT):
        for f in files:
            ext = f.rsplit(".", 1)[-1].lower()
            if ext not in _AUDIO_EXTS:
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, LIB_ROOT)
            artist = title = album = None
            try:
                m = M(p, easy=True)
                artist = (m.get("artist") or [None])[0]
                title = (m.get("title") or [None])[0]
                album = (m.get("album") or [None])[0]
            except Exception:
                pass
            if not artist or not title:
                parts = rel.split(os.sep)
                if len(parts) >= 2:
                    artist = artist or parts[0]
                    title = title or os.path.splitext(parts[-1])[0]
                    album = album or (parts[1] if len(parts) > 2 else None)
                else:
                    continue
            entries.append({"path": rel, "artist": artist, "title": title, "album": album})
    with open(_LOCAL_CACHE, "w") as fh:
        json.dump(entries, fh)
    return entries


def _local_fallback(sub, artist: str, title: str):
    """Find the track on disk, then resolve it to a Navidrome song id."""
    index = _load_local_index()
    av = _artist_set(artist)
    want_t = _strong_norm(title)
    want_t_flex = flex_title(title)
    best, best_r = None, 0.0
    for e in index:
        if not _artist_match(e.get("artist", ""), av):
            continue
        t_strong = _strong_norm(e.get("title"))
        t_flex = flex_title(e.get("title"))
        r = difflib.SequenceMatcher(None, t_flex, want_t_flex).ratio()
        if t_strong == want_t or t_flex == want_t_flex:
            best, best_r = e, 1.0
            break
        if r > best_r:
            best, best_r = e, r
    if best is None or best_r < 0.8:
        # Title-only pass: artist credit mismatch (e.g. Qobuz release credited
        # to a remix artist). Require near-exact title to avoid wrong covers.
        cands = []
        for e in index:
            t_strong = _strong_norm(e.get("title"))
            t_flex = flex_title(e.get("title"))
            if t_strong == want_t or t_flex == want_t_flex:
                cands.append((e, 1.0))
            else:
                r = difflib.SequenceMatcher(None, t_flex, want_t_flex).ratio()
                if r >= 0.95:
                    cands.append((e, r))
        if cands:
            tokens = set(_strong_norm(artist).split())
            def _artist_overlap(e):
                ea = _strong_norm(e.get("artist", ""))
                return any(len(tk) >= 4 and tk in ea.split() for tk in tokens) or \
                       any(len(tk) >= 4 and tk in ea for tk in tokens)
            unique = len(cands) == 1
            for e, r in cands:
                if unique or _artist_overlap(e):
                    best, best_r = e, r
                    break
            if best is None or best_r < 0.8:
                return None, None
        else:
            return None, None
    # Resolve to Navidrome id via a re-search with the on-disk title/artist.
    for a in (best.get("artist"), first_artist(best.get("artist") or "")):
        for s in sub.search_songs(a or "", best.get("title", ""), limit=15):
            if _strong_norm(s.get("title")) == _strong_norm(best.get("title")):
                return s["id"], f"local{best_r:.2f}"
    # last resort: title-only search
    for s in sub.search_songs("", best.get("title", ""), limit=15):
        if _strong_norm(s.get("title")) == _strong_norm(best.get("title")):
            return s["id"], f"local{best_r:.2f}"
    return None, None


def find_song(sub, artist: str, title: str, limit: int = 30):
    """Return (song_id, how) or (None, None)."""
    fa = first_artist(artist)
    fa_n = _strong_norm(fa).replace(" ", "")

    search_artists = []
    for a in (artist, fa, fa_n):
        a = (a or "").strip()
        if a and a not in search_artists:
            search_artists.append(a)
    search_artists.append("")  # title-only

    title_variants = []
    for t in (title, _strip_feat(title), strip_versions(_strip_feat(title))):
        t = (t or "").strip()
        if t and t not in title_variants:
            title_variants.append(t)

    for sa in search_artists:
        for tv in title_variants:
            hits = sub.search_songs(sa, tv, limit=limit)
            if hits:
                res = _match_candidates(hits, artist, title)
                if res[0]:
                    return res
    return _local_fallback(sub, artist, title)
