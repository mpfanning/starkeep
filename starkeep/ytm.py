"""YouTube Music fallback downloader for starkeep.

Fallback source for tracks Qobuz cannot serve (repeated connection drops,
streaming-only releases, not found). Output is ~250kbps AAC .m4a, placed
with the same library layout as qobuz.download_track:

    <library_dir>/<Artist>/<Album>/NN. Title.m4a
    <library_dir>/<Artist> - <Title>.m4a      (when album is unknown)

Requires yt-dlp and ffmpeg on PATH. Used via download_with_fallback(),
which tries Qobuz first and only hits YouTube Music on Qobuz failure.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

YTDL = shutil.which("yt-dlp")

# YouTube Music search filter: Songs only (base64 "EAE=")
_SONGS_FILTER = "EAE%3D"


def _norm(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ytm_search(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube Music (songs only).

    Full extraction (not --flat-playlist) so artist/channel/album come back;
    flat entries carry no channel. Slow (~4-5s per entry) but acceptable for
    a fallback path that only runs when Qobuz failed.

    Returns [{id, title, artist, album, thumbnail, duration}].
    """
    if not YTDL:
        raise RuntimeError("yt-dlp not found on PATH (brew install yt-dlp)")
    url = f"https://music.youtube.com/search?q={quote_plus(query)}&sp={_SONGS_FILTER}"
    r = _run(
        [YTDL, "--no-playlist", "--playlist-end", str(limit),
         "--no-warnings", "--dump-json", url],
        timeout=300,
    )
    entries: list[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not d.get("id"):
            continue
        entries.append({
            "id": d["id"],
            "title": d.get("title") or "",
            "artist": d.get("artist") or d.get("channel") or d.get("uploader") or "",
            "album": d.get("album") or "",
            "thumbnail": d.get("thumbnail") or "",
            "duration": d.get("duration") or 0,
        })
    return entries


def _score_entry(entry: dict, artist: str, title: str) -> int:
    na, nt = _norm(artist), _norm(title)
    et = _norm(entry["title"])
    ea = _norm(entry["artist"])
    score = 0
    if et == nt:
        score += 60
    elif nt in et or et in nt:
        score += 35
    if na:
        if ea == na or na in ea or ea in na:
            score += 35
        elif na in et:
            # "Artist - Title" embedded in the video title
            score += 15
    if entry.get("duration") and 60 <= entry["duration"] <= 900:
        score += 5
    return score


def _pick_best(entries: list[dict], artist: str, title: str,
               threshold: int = 60) -> dict | None:
    best, best_score = None, -1
    for e in entries:
        s = _score_entry(e, artist, title)
        if s > best_score:
            best, best_score = e, s
    if best is None or best_score < threshold:
        return None
    return best


def _tag_m4a(path: Path, artist: str, title: str, album: str,
             track_number: int | None, thumbnail_url: str | None = None) -> None:
    """Write ID3/MP4 tags (+ cover). Best-effort: never fails the download."""
    try:
        from mutagen.mp4 import MP4, MP4Cover
        m = MP4(str(path))
        m["\xa9ART"] = [artist]
        m["\xa9nam"] = [title]
        if album:
            m["\xa9alb"] = [album]
        if track_number:
            m["trkn"] = [(track_number, 0)]
        if thumbnail_url:
            urls = [thumbnail_url]
            if "maxresdefault" in thumbnail_url:
                urls.append(thumbnail_url.replace("maxresdefault", "hqdefault"))
            for u in urls:
                try:
                    import requests
                    r = requests.get(u, timeout=30)
                    if r.ok:
                        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                        fmt = {
                            "image/jpeg": MP4Cover.FORMAT_JPEG,
                            "image/png": MP4Cover.FORMAT_PNG,
                            "image/webp": MP4Cover.FORMAT_WEBP,
                        }.get(mime, MP4Cover.FORMAT_JPEG)
                        m["covr"] = [MP4Cover(r.content, imageformat=fmt)]
                        break
                except Exception:
                    continue  # try next URL
        m.save()
    except Exception as e:
        log.warning("ytm tagging failed for %s: %s", path.name, e)


def ytm_download(query: str, library_dir: str,
                 qobuz_meta: dict | None = None, dry_run: bool = False) -> dict:
    """Download a track from YouTube Music.

    ``query`` is "Artist - Title" or just "Title".
    ``qobuz_meta`` is an optional {album, track_number} dict from a Qobuz
    metadata match, used for library placement and tagging even when the
    Qobuz *download* itself failed (streaming-only releases still expose
    album/track metadata via search).

    Idempotent: an existing non-empty destination file is skipped; a
    zero-byte leftover (interrupted Qobuz download) is removed and
    re-downloaded.

    Returns the same shape as qobuz.download_track plus source='youtube_music'.
    """
    if not YTDL:
        raise RuntimeError("yt-dlp not found on PATH (brew install yt-dlp)")

    artist, title = "", query.strip()
    if " - " in query:
        artist, title = (s.strip() for s in query.split(" - ", 1))

    entries = ytm_search(f"{artist} {title}".strip())
    if not entries:
        raise RuntimeError(f"no YouTube Music results for '{query}'")
    best = _pick_best(entries, artist, title)
    if best is None:
        raise RuntimeError(f"no good YouTube Music match for '{query}'")

    artist_name = (artist or best["artist"]).strip()
    meta = qobuz_meta or {}
    album = meta.get("album") or best.get("album") or ""
    tno = meta.get("track_number")

    ext = ".m4a"
    artist_s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", artist_name).strip()[:80]
    title_s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip()
    stem = f"{tno:02d}. {title_s}" if tno else title_s
    if album:
        album_s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", album).strip()[:80]
        dest = Path(library_dir).expanduser() / artist_s / album_s / f"{stem}{ext}"
    else:
        dest = Path(library_dir).expanduser() / f"{artist_s} - {title_s}{ext}"

    # Idempotency + zero-byte cleanup
    for ext2 in (".m4a", ".flac", ".mp3"):
        p = dest.with_suffix(ext2)
        if p.exists():
            if p.stat().st_size == 0:
                p.unlink()
                log.info("removed zero-byte leftover: %s", p)
            else:
                log.info("ytm skip (exists): %s", p)
                return {"artist": artist_name, "title": title, "album": album,
                        "track_number": tno, "dest": p, "downloaded": False,
                        "source": "youtube_music"}
    if dry_run:
        log.info("ytm dry run: %s", dest)
        return {"artist": artist_name, "title": title, "album": album,
                "track_number": tno, "dest": dest, "downloaded": False,
                "source": "youtube_music"}

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="ytm_"))
    try:
        r = _run(
            [YTDL, "-x", "--audio-format", "m4a",
             "--audio-quality", "256K", "--no-playlist", "--no-warnings",
             "--no-overwrites",
             "-o", str(tmpdir / "%(id)s.%(ext)s"),
             f"https://music.youtube.com/watch?v={best['id']}"],
            timeout=900,
        )
        if r.returncode != 0:
            raise RuntimeError(f"yt-dlp download failed: {r.stderr.strip()[:300]}")
        downloaded = list(tmpdir.glob("*.m4a"))
        if not downloaded:
            raise RuntimeError("yt-dlp produced no .m4a output")
        shutil.move(str(downloaded[0]), str(dest))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    _tag_m4a(dest, artist_name, title, album, tno, best.get("thumbnail"))
    log.info("ytm downloaded: %s (%.0f KB)", dest.name,
             dest.stat().st_size / 1024)
    return {"artist": artist_name, "title": title, "album": album,
            "track_number": tno, "dest": dest, "downloaded": True,
            "source": "youtube_music"}


def download_with_fallback(query: str, library_dir: str, dry_run: bool = False) -> dict:
    """Qobuz first; on failure, fall back to YouTube Music.

    When Qobuz has a metadata match but the download fails (streaming-only
    releases, connection drops), the Qobuz album/track_number is reused for
    YTM placement so the file lands in the right album folder.

    Returns the qobuz.download_track result shape plus a ``source`` key
    ("qobuz" or "youtube_music"); a failed Qobuz attempt leaves a
    ``qobuz_error`` key.
    """
    from .qobuz import _norm, QobuzClient, download_track

    try:
        res = download_track(query, library_dir, dry_run=dry_run)
        res.setdefault("source", "qobuz")
        return res
    except Exception as e:
        qobuz_err = str(e)
        if dry_run:
            raise
        log.warning("Qobuz failed for %r: %s — trying YouTube Music", query, qobuz_err)

    # Best-effort Qobuz metadata for placement (search works even when the
    # download URL does not).
    meta: dict | None = None
    try:
        client = QobuzClient()
        a, t = "", query.strip()
        if " - " in query:
            a, t = (s.strip() for s in query.split(" - ", 1))
        tracks = client.search_tracks(f"{a} {t}".strip(), limit=15)
        na, nt = _norm(a), _norm(t)
        cands = [x for x in tracks
                 if _norm(x["title"]) == nt and (not na or _norm(x["artist"]) == na)]
        if cands:
            meta = {"album": cands[0].get("album") or "",
                    "track_number": cands[0].get("track_number")}
    except Exception:
        pass

    try:
        res = ytm_download(query, library_dir, qobuz_meta=meta)
        res["qobuz_error"] = qobuz_err
        return res
    except Exception as ytm_err:
        raise RuntimeError(
            f"Qobuz failed ({qobuz_err}); YouTube Music fallback failed ({ytm_err})"
        )
