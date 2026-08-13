import json
import os
import re
import subprocess
import time
import uuid

import requests
import yt_dlp
from RedDownloader import RedDownloader

try:
    from moviepy import VideoFileClip, AudioFileClip
except ImportError:
    from moviepy.editor import VideoFileClip, AudioFileClip

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _kind_for_ext(ext):
    ext = (ext or "").lower()
    if ext in IMAGE_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return "document"


def _resolve_reddit_share_link(url):
    # Reddit share links (/r/sub/s/xxxxx) redirect to the real permalink.
    # Resolve them up front so RedDownloader/gallery-dl/yt-dlp all get the
    # canonical URL instead of a short link some of them can't parse.
    if "/s/" not in url:
        return url
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, allow_redirects=True, timeout=15, stream=True)
        resolved = resp.url
        resp.close()
        if resolved and resolved != url:
            return resolved.split("?")[0]
    except Exception:
        pass
    return url


def _with_429_retry(fn, retries=1, delay=5):
    # Call fn() -> (items, status). If status mentions a 429, wait and retry once.
    items, status = fn()
    attempt = 0
    while not items and "429" in status and attempt < retries:
        time.sleep(delay)
        items, status = fn()
        attempt += 1
    return items, status


# ---------- Reddit (RedDownloader) ----------

def extract_reddit(url, workdir):
    os.makedirs(workdir, exist_ok=True)
    dest = workdir if workdir.endswith("/") else workdir + "/"
    try:
        RedDownloader.Download(url, output="media", destination=dest, verbose=False)
    except Exception as e:
        return [], f"RedDownloader error: {e}"

    files = []
    for root, _dirs, fs in os.walk(workdir):
        for f in fs:
            # Skip moviepy's intermediate temp artifacts from an interrupted
            # merge (e.g. "mediaTEMP_MPY_wvf_snd.mp3") -- these are leftover
            # pieces, not real output, and sending them alongside/instead of
            # the actual merged file produces the "video and audio arrived
            # as separate files" bug.
            if "TEMP_MPY" in f:
                continue
            files.append(os.path.join(root, f))

    items = [
        {"kind": _kind_for_ext(os.path.splitext(f)[1]), "source": "file", "value": f}
        for f in files
    ]
    status = f"{len(items)} file(s) downloaded."
    return items, status


# ---------- Direct RedGifs API (no yt-dlp/gallery-dl needed) ----------
# Based on the approach used by bulk-downloader-for-reddit: hit RedGifs' own
# public API directly with a temporary auth token, rather than relying on a
# third-party extractor that breaks whenever RedGifs changes their site.

def _redgifs_id(url):
    cleaned = url.rstrip("/")
    match = re.match(r".*/(.*?)(?:#.*|\?.*|\..{0,})?$", cleaned)
    if not match:
        raise ValueError(f"Could not extract RedGifs ID from {url}")
    rid = match.group(1).lower()
    if rid.endswith("-mobile"):
        rid = rid[: -len("-mobile")]
    return rid


def extract_redgifs_direct(url):
    try:
        redgif_id = _redgifs_id(url)
    except Exception as e:
        return [], f"RedGifs ID error: {e}"

    try:
        auth_resp = requests.get("https://api.redgifs.com/v2/auth/temporary", timeout=15)
        auth_token = auth_resp.json().get("token")
        if not auth_token:
            return [], "Could not get RedGifs auth token"
    except Exception as e:
        return [], f"RedGifs auth error: {e}"

    headers = {
        "referer": "https://www.redgifs.com/",
        "origin": "https://www.redgifs.com",
        "content-type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    }

    try:
        resp = requests.get(f"https://api.redgifs.com/v2/gifs/{redgif_id}", headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        return [], f"RedGifs API error: {e}"

    items = []
    try:
        gif = data["gif"]
        if gif["type"] == 1:  # video
            hd = gif["urls"].get("hd")
            sd = gif["urls"].get("sd")
            chosen = None
            if hd:
                try:
                    if requests.get(hd, headers=headers, timeout=15).ok:
                        chosen = hd
                except Exception:
                    pass
            chosen = chosen or sd
            if chosen:
                items.append({"kind": "video", "source": "url", "value": chosen})
        elif gif["type"] == 2:  # image / gallery
            if gif.get("gallery"):
                g_resp = requests.get(
                    f"https://api.redgifs.com/v2/gallery/{gif['gallery']}", headers=headers, timeout=15
                )
                g_data = g_resp.json()
                for p in g_data.get("gifs", []):
                    u = p.get("urls", {}).get("hd")
                    if u:
                        items.append({"kind": "photo", "source": "url", "value": u})
            else:
                u = gif["urls"].get("hd")
                if u:
                    items.append({"kind": "photo", "source": "url", "value": u})
    except (KeyError, TypeError) as e:
        return [], f"RedGifs response parsing error: {e}"

    return items, f"{len(items)} item(s) found via RedGifs API."


# ---------- Video+audio merge helper ----------
# Reddit (and some other sites) serve video as separate video-only and
# audio-only streams. yt-dlp's -g style URL extraction returns both as
# distinct URLs without merging -- sending them separately means a silent
# video. This downloads both and merges them with moviepy (same library
# RedDownloader already uses under the hood, so no extra system ffmpeg
# dependency needed).

def _download_stream(url, path):
    resp = requests.get(url, headers=BROWSER_HEADERS, stream=True, timeout=60)
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)


def merge_video_audio(video_url, audio_url, workdir):
    os.makedirs(workdir, exist_ok=True)
    tag = uuid.uuid4().hex
    vid_path = os.path.join(workdir, f"v_{tag}.mp4")
    aud_path = os.path.join(workdir, f"a_{tag}.mp4")
    out_path = os.path.join(workdir, f"merged_{tag}.mp4")

    try:
        _download_stream(video_url, vid_path)
        _download_stream(audio_url, aud_path)
        video_clip = VideoFileClip(vid_path)
        audio_clip = AudioFileClip(aud_path)
        final = video_clip.with_audio(audio_clip) if hasattr(video_clip, "with_audio") else video_clip.set_audio(audio_clip)
        final.write_videofile(out_path, codec="libx264", audio_codec="aac", logger=None)
        video_clip.close()
        audio_clip.close()
        final.close()
        return out_path
    except Exception:
        return None


def _pick_best_format(formats, want):
    best = None
    best_score = -1
    for fmt in formats:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        is_video_only = vcodec not in (None, "none") and acodec in (None, "none")
        is_audio_only = acodec not in (None, "none") and vcodec in (None, "none")
        if want == "video" and is_video_only and fmt.get("url"):
            score = fmt.get("height") or 0
            if score > best_score:
                best_score, best = score, fmt
        elif want == "audio" and is_audio_only and fmt.get("url"):
            score = fmt.get("abr") or 0
            if score > best_score:
                best_score, best = score, fmt
    return best


# ---------- Generic (yt-dlp) ----------

def _collect_ytdlp_urls(entry):
    found = []
    for fmt in entry.get("formats") or []:
        ext = (fmt.get("ext") or "").lower()
        if ext in IMAGE_EXTS and fmt.get("url"):
            found.append(("photo", fmt["url"]))
        elif ext in VIDEO_EXTS and fmt.get("vcodec") not in (None, "none") and fmt.get("url"):
            found.append(("video", fmt["url"]))

    if (entry.get("ext") or "").lower() in IMAGE_EXTS and entry.get("url"):
        found.append(("photo", entry["url"]))

    if not found:
        thumb = entry.get("thumbnail")
        if thumb:
            found.append(("photo", thumb))

    return found


def extract_ytdlp(url, workdir=None, allow_merge=False):
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return [], f"yt-dlp error: {e}"

    if not info:
        return [], "yt-dlp returned nothing."

    entries = [e for e in (info.get("entries") or [info]) if e]

    items = []
    seen = set()
    for entry in entries:
        formats = entry.get("formats") or []
        has_muxed = any(
            (f.get("vcodec") not in (None, "none")) and (f.get("acodec") not in (None, "none"))
            for f in formats
        )

        if not has_muxed:
            video_fmt = _pick_best_format(formats, "video")
            audio_fmt = _pick_best_format(formats, "audio")
            if video_fmt and audio_fmt:
                if allow_merge and workdir:
                    merged_path = merge_video_audio(video_fmt["url"], audio_fmt["url"], workdir)
                    if merged_path:
                        items.append({"kind": "video", "source": "file", "value": merged_path})
                # Whether merge succeeded, failed, or wasn't allowed here (e.g.
                # Render, which can't safely do this in 512MB), we do NOT fall
                # through to sending the raw unmerged streams -- that produces
                # a silent video, which is worse than no video. If nothing was
                # appended above, this entry is simply skipped, and the caller
                # (process_url) will see 0 items and fall back to GitHub,
                # which has the RAM to merge safely.
                continue

        for kind, u in _collect_ytdlp_urls(entry):
            if u not in seen:
                seen.add(u)
                items.append({"kind": kind, "source": "url", "value": u})

    status = f"{len(entries)} entrie(s), {len(items)} media item(s) found."
    return items, status


# ---------- Generic fallback (gallery-dl) ----------

def extract_gallery_dl(url):
    try:
        result = subprocess.run(
            ["gallery-dl", "-g", url], capture_output=True, text=True, timeout=90
        )
    except Exception as e:
        return [], f"gallery-dl error: {e}"

    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    items = []
    for u in urls:
        if u.split("?")[0] == url.split("?")[0]:
            # gallery-dl's generic extractor sometimes echoes the input page
            # back when it can't identify real media -- that's not a result.
            continue
        ext = os.path.splitext(u.split("?")[0])[1]
        items.append({"kind": _kind_for_ext(ext) or "photo", "source": "url", "value": u})

    status = f"{len(items)} item(s) found."
    return items, status


def _reddit_redgifs_redirect(url):
    """If a Reddit post's content is actually hosted on RedGifs (post.domain
    == 'redgifs.com'), return that RedGifs URL so we can skip straight to
    the RedGifs API instead of running the whole RedDownloader/gallery-dl
    chain against a Reddit post that was never going to have local media.
    Returns None on any failure or uncertainty -- callers should just
    proceed with the normal chain in that case, nothing changes."""
    try:
        check_url = url if url.endswith(".json") else url.rstrip("/") + ".json"
        resp = requests.get(check_url, headers=BROWSER_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        post = data[0]["data"]["children"][0]["data"]
        domain = (post.get("domain") or "").lower()
        if "redgifs.com" in domain:
            return post.get("url_overridden_by_dest") or post.get("url")
    except Exception:
        pass
    return None


# ---------- Dispatcher ----------

def extract_media(url, workdir):
    url = url.strip()

    if "reddit.com" in url and "/s/" in url:
        url = _resolve_reddit_share_link(url)

    if "redgifs.com" in url:
        items, status = extract_redgifs_direct(url)
        if items:
            return items, f"[RedGifs API] {status}"

        items, status = _with_429_retry(lambda: extract_ytdlp(url, workdir))
        if items:
            return items, f"[RedGifs API failed] | [yt-dlp/redgifs] {status}"
        g_items, g_status = extract_gallery_dl(url)
        return g_items, f"[RedGifs API failed] | [yt-dlp/redgifs] {status} | [gallery-dl fallback] {g_status}"

    if "reddit.com" in url or "redd.it" in url:
        redgifs_url = _reddit_redgifs_redirect(url)
        if redgifs_url:
            items, status = extract_redgifs_direct(redgifs_url)
            if items:
                return items, f"[RedGifs API via Reddit redirect] {status}"
            # Fall through to the normal chain if the RedGifs API attempt
            # itself failed -- don't just give up because of this shortcut.

        items, status = _with_429_retry(lambda: extract_reddit(url, workdir))
        if items:
            return items, f"[RedDownloader] {status}"

        # RedDownloader misses some cases (e.g. GIFs inside galleries, which
        # Reddit serves as looping MP4s or links out to RedGifs). gallery-dl
        # can usually resolve those in one shot.
        g_items, g_status = _with_429_retry(lambda: extract_gallery_dl(url))
        if g_items:
            return g_items, f"[RedDownloader] {status} | [gallery-dl fallback] {g_status}"

        # Last resort: yt-dlp, with our own video+audio merge for Reddit's
        # separate-stream videos instead of sending a silent video.
        yt_items, yt_status = extract_ytdlp(url, workdir)
        return yt_items, f"[RedDownloader] {status} | [gallery-dl] {g_status} | [yt-dlp fallback] {yt_status}"

    items, status = extract_ytdlp(url, workdir)
    if items:
        return items, f"[yt-dlp] {status}"

    g_items, g_status = extract_gallery_dl(url)
    return g_items, f"[yt-dlp] {status} | [gallery-dl] {g_status}"
