"""Daily cron: upgrade YTM (m4a) files to Qobuz FLAC when available.

Scans the library for .m4a files (YTM fallback output), attempts to download
the FLAC version from Qobuz, replaces the m4a if successful, then triggers a
Navidrome rescan and updates affected playlists.

Run:  python -m starkeep.upgrade_flac
"""

from __future__ import annotations

import fcntl
import logging
import sys
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path.home() / "Library" / "Logs" / "starkeep"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "upgrade-flac.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("upgrade_flac")

LOCK_FILE = Path("/tmp/starkeep_qobuz_upgrade.lock")

# Non-FLAC extensions to scan for
NON_FLAC_EXTS = {".m4a", ".mp3", ".aac", ".ogg", ".opus", ".webm"}


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _load_config():
    """Load starkeep config + env secrets."""
    from starkeep.config import load, _load_starkeep_env
    import os

    _load_starkeep_env()
    cfg = load(Path.home() / ".starkeep")
    if cfg is None:
        raise RuntimeError("No config.yaml found at ~/.starkeep/")
    # Pull subsonic credentials from env (set by _load_starkeep_env)
    if not cfg.subsonic_password:
        cfg.subsonic_password = os.environ.get("SUBSONIC_PASSWORD", "")
    if not cfg.subsonic_user:
        cfg.subsonic_user = os.environ.get("SUBSONIC_USER", "mf")
    if not cfg.subsonic_url:
        cfg.subsonic_url = "http://127.0.0.1:4533"
    return cfg


def find_non_flac_files(library_dir: str) -> list[Path]:
    """Walk library and return all non-FLAC audio files."""
    results = []
    root = Path(library_dir).expanduser()
    for p in root.rglob("*"):
        if p.suffix.lower() in NON_FLAC_EXTS and p.stat().st_size > 0:
            results.append(p)
    return sorted(results)


def read_tags(path: Path) -> tuple[str, str]:
    """Read artist/title from audio file tags via mutagen."""
    from mutagen import File as MutagenFile

    m = MutagenFile(str(path), easy=True)
    if m is None:
        return "", ""
    artist = (m.get("artist") or [""])[0].strip()
    title = (m.get("title") or [""])[0].strip()
    return artist, title


def find_old_song_ids(sub, tracks: list[dict]) -> dict[str, str]:
    """For each track, find its current song_id in Navidrome.

    Returns {artist|title: song_id}
    """
    id_map = {}
    for tr in tracks:
        key = _norm(tr["artist"]) + "|" + _norm(tr["title"])
        try:
            songs = sub.search_songs(tr["artist"], tr["title"], limit=10)
        except Exception as e:
            log.warning("search3 failed for %s: %s", key, e)
            continue
        # Prefer match whose path contains the m4a filename
        m4a_name = Path(tr["path"]).name
        for s in songs:
            s_path = s.get("path", "") or ""
            if m4a_name in s_path or (
                _norm(s.get("artist", "")) == _norm(tr["artist"])
                and _norm(s.get("title", "")) == _norm(tr["title"])
            ):
                id_map[key] = s["id"]
                break
    return id_map


def get_playlists_containing(sub, song_id: str) -> list[dict]:
    """Return list of playlists that contain the given song_id."""
    results = []
    for pl in sub.playlists():
        try:
            data = sub._call("getPlaylist", id=pl["id"])
            entries = data.get("playlist", {}).get("entry", [])
            entry_ids = [e.get("id") or e.get("songId") for e in entries]
            if song_id in entry_ids:
                results.append(pl)
        except Exception:
            continue
    return results


def find_new_song_id(sub, artist: str, title: str) -> str | None:
    """After rescan, find the new song_id for the FLAC version."""
    try:
        songs = sub.search_songs(artist, title, limit=10)
    except Exception as e:
        log.warning("post-rescan search failed for %s - %s: %s", artist, title, e)
        return None
    for s in songs:
        s_path = (s.get("path") or "").lower()
        if s_path.endswith(".flac"):
            if _norm(s.get("artist", "")) == _norm(artist) and _norm(s.get("title", "")) == _norm(title):
                return s["id"]
    # Fallback: first exact artist+title match
    for s in songs:
        if _norm(s.get("artist", "")) == _norm(artist) and _norm(s.get("title", "")) == _norm(title):
            return s["id"]
    return None


def update_playlists(sub, replacements: list[dict]) -> None:
    """For each replacement, ensure the new FLAC song_id is in the playlists
    that previously had the old m4a song_id."""
    for rep in replacements:
        old_id = rep.get("old_song_id")
        new_id = rep.get("new_song_id")
        if not new_id:
            log.info("  [skip] no new song_id found for %s - %s", rep["artist"], rep["title"])
            continue
        affected = rep.get("affected_playlists", [])
        if not affected:
            log.info("  [ok] %s - %s: not in any playlist, nothing to update", rep["artist"], rep["title"])
            continue
        for pl in affected:
            try:
                data = sub._call("getPlaylist", id=pl["id"])
                entries = data.get("playlist", {}).get("entry", [])
                new_ids = []
                for e in entries:
                    eid = e.get("id") or e.get("songId")
                    if eid == old_id:
                        # Replace in-place with new_id
                        new_ids.append(new_id)
                    else:
                        new_ids.append(eid)
                # If old_id wasn't found (already gone), just add new_id
                if old_id and old_id not in [e.get("id") or e.get("songId") for e in entries]:
                    new_ids.append(new_id)
                # Deduplicate
                seen = set()
                deduped = []
                for sid in new_ids:
                    if sid not in seen:
                        seen.add(sid)
                        deduped.append(sid)
                sub.replace_playlist(pl.get("name") or pl.get("playlistName", ""), deduped)
                log.info("  updated playlist '%s' (%d songs)", pl.get("name"), len(deduped))
            except Exception as e:
                log.warning("  failed to update playlist '%s': %s", pl.get("name"), e)


def main() -> int:
    # --- Lock: prevent concurrent Qobuz access ---
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another Qobuz operation is running (lock held). Exiting.")
        lock_fd.close()
        return 0

    try:
        cfg = _load_config()
        library_dir = cfg.library_dir
        log.info("=== Upgrade m4a → FLAC run started ===")
        log.info("Library: %s", library_dir)

        # --- Phase 1: Scan ---
        non_flac = find_non_flac_files(library_dir)
        log.info("Found %d non-FLAC file(s)", len(non_flac))
        if not non_flac:
            log.info("Nothing to do.")
            return 0

        tracks = []
        for p in non_flac:
            artist, title = read_tags(p)
            if not artist or not title:
                log.info("  skip (no tags): %s", p.name)
                continue
            tracks.append({"path": str(p), "artist": artist, "title": title})
            log.info("  candidate: %s - %s (%s)", artist, title, p.name)

        if not tracks:
            log.info("No tagged files to process.")
            return 0

        # --- Phase 2: Get old song IDs + affected playlists (BEFORE changes) ---
        sub = None
        old_id_map = {}
        affected_map = {}  # key -> [playlists]
        try:
            from starkeep.subsonic import SubsonicClient
            sub = SubsonicClient(cfg.subsonic_url, cfg.subsonic_user, cfg.subsonic_password)
            sub.ping()
            old_id_map = find_old_song_ids(sub, tracks)
            for key, sid in old_id_map.items():
                affected_map[key] = get_playlists_containing(sub, sid)
                log.info("  %s | %s → song_id=%s in %d playlist(s)",
                         key, sid, len(affected_map[key]))
        except Exception as e:
            log.warning("Could not query Navidrome for playlist info: %s", e)
            sub = None

        # --- Phase 3: Attempt Qobuz downloads ---
        from starkeep.qobuz import download_track

        replacements = []
        for tr in tracks:
            query = f"{tr['artist']} - {tr['title']}"
            key = _norm(tr["artist"]) + "|" + _norm(tr["title"])
            try:
                res = download_track(query, library_dir)
                if res["downloaded"]:
                    log.info("  [FLAC downloaded] %s → %s", tr["title"], res["dest"])
                    # Delete the old m4a
                    m4a_path = Path(tr["path"])
                    m4a_path.unlink()
                    log.info("  [deleted m4a] %s", m4a_path.name)
                    replacements.append({
                        "artist": tr["artist"],
                        "title": tr["title"],
                        "old_path": tr["path"],
                        "new_path": str(res["dest"]),
                        "old_song_id": old_id_map.get(key),
                        "affected_playlists": affected_map.get(key, []),
                    })
                elif res["downloaded"] is False:
                    # FLAC already exists — m4a is redundant
                    m4a_path = Path(tr["path"])
                    m4a_path.unlink()
                    log.info("  [FLAC already existed, removed m4a] %s", m4a_path.name)
                    replacements.append({
                        "artist": tr["artist"],
                        "title": tr["title"],
                        "old_path": tr["path"],
                        "new_path": str(res["dest"]),
                        "old_song_id": old_id_map.get(key),
                        "affected_playlists": affected_map.get(key, []),
                    })
            except RuntimeError as e:
                log.info("  [skip] %s: %s", tr["title"], e)
            except Exception as e:
                log.warning("  [error] %s: %s", tr["title"], e)
            time.sleep(3)  # Qobuz CDN courtesy

        # --- Phase 4: Navidrome rescan + playlist updates ---
        if replacements:
            log.info("%d file(s) upgraded. Triggering Navidrome rescan…", len(replacements))
            if sub is None:
                try:
                    from starkeep.subsonic import SubsonicClient
                    sub = SubsonicClient(cfg.subsonic_url, cfg.subsonic_user, cfg.subsonic_password)
                except Exception as e:
                    log.error("Cannot connect to Navidrome for rescan: %s", e)
                    return 1
            try:
                if sub.start_scan():
                    log.info("Rescan started, waiting…")
                    sub.wait_for_scan(timeout=600)
                    log.info("Rescan complete.")
                else:
                    log.warning("startScan not supported; skipping rescan.")
            except Exception as e:
                log.error("Rescan failed: %s", e)

            # Find new song IDs and update playlists
            time.sleep(5)  # give Navidrome a moment to index
            for rep in replacements:
                new_id = find_new_song_id(sub, rep["artist"], rep["title"])
                rep["new_song_id"] = new_id
                if new_id:
                    log.info("  new song_id: %s - %s → %s", rep["artist"], rep["title"], new_id)

            update_playlists(sub, replacements)
        else:
            log.info("No files upgraded this run.")

        log.info("=== Upgrade run complete: %d upgraded ===", len(replacements))
        return 0

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
