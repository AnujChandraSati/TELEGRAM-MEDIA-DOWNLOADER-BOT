import logging
import mimetypes
import multiprocessing
import os
import re
import shutil
import threading
import uuid

import requests
from flask import Flask, jsonify, request

from extractor import extract_media
from github_dispatch import dispatch_to_github
from telegram_utils import (
    delete_message,
    send_document,
    send_message,
    send_photo,
    send_video,
    set_webhook,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


REFERER_MAP = {
    "redd.it": "https://www.reddit.com/",
    "reddit.com": "https://www.reddit.com/",
    "redgifs.com": "https://www.redgifs.com/",
    "twimg.com": "https://twitter.com/",
    "cdninstagram.com": "https://www.instagram.com/",
}


def _headers_for_url(url):
    headers = dict(BROWSER_HEADERS)
    for domain, referer in REFERER_MAP.items():
        if domain in url:
            headers["Referer"] = referer
            break
    return headers


def _resolve_reddit_share_link(url):
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


URL_RE = re.compile(r"https?://\S+")

_recently_seen = set()
_MAX_SEEN = 2000


def already_seen(chat_id, message_id):
    key = f"{chat_id}:{message_id}"
    if key in _recently_seen:
        return True
    _recently_seen.add(key)
    if len(_recently_seen) > _MAX_SEEN:
        _recently_seen.clear()
    return False


def find_url(text):
    """Pull the first URL out of a message, even if it's surrounded by other
    text/formatting (e.g. Reddit's native 'Share -> Telegram' button)."""
    match = URL_RE.search(text)
    return match.group(0).rstrip(").,!?") if match else None


def _download_url_to_file(url, workdir, idx):
    # Download a remote media URL server-side (with browser + referer headers)
    # so Telegram never has to fetch it directly -- avoids hotlink protection /
    # missing-header rejections from Reddit/CDN sources.
    os.makedirs(workdir, exist_ok=True)
    try:
        resp = requests.get(url, headers=_headers_for_url(url), stream=True, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to download %s: %s", url, e)
        return None

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()

    # Reject block pages / interstitials that came back as HTML instead of media.
    if content_type.startswith("text/") or "html" in content_type:
        logger.warning("Rejected non-media response for %s (content-type: %s)", url, content_type)
        return None

    ext = mimetypes.guess_extension(content_type) or os.path.splitext(url.split("?")[0])[1] or ".bin"
    path = os.path.join(workdir, f"item_{idx}{ext}")

    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except Exception as e:
        logger.warning("Failed to save %s: %s", url, e)
        return None

    if os.path.getsize(path) < 256:
        # Suspiciously small -- likely an error page or empty response body.
        logger.warning("Downloaded file for %s is too small, discarding", url)
        return None

    return path

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme")
BASE_URL = os.environ.get("BASE_URL")  # e.g. https://your-app.onrender.com
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "10"))

app = Flask(__name__)

# Register the webhook with Telegram once at startup (idempotent, safe to
# call on every worker boot). Only runs if BASE_URL is set.
if BASE_URL:
    _webhook_url = f"{BASE_URL.rstrip('/')}/webhook/{WEBHOOK_SECRET}"
    _result = set_webhook(BOT_TOKEN, _webhook_url)
    logger.info("setWebhook -> %s : %s", _webhook_url, _result)


LOCAL_TIMEOUT_SECONDS = 45  # images/galleries finish well within this; slow
                            # video encodes on Render's weak CPU won't, and
                            # get killed and handed to GitHub instead.


def _watch_and_fallback(proc, url, chat_id, message_id):
    proc.join(timeout=LOCAL_TIMEOUT_SECONDS)
    if proc.is_alive():
        logger.warning(
            "Local processing exceeded %ss, killing and falling back to GitHub: %s",
            LOCAL_TIMEOUT_SECONDS, url,
        )
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
        dispatch_to_github(url, chat_id, message_id)


@app.route("/", methods=["GET"])
def health():
    # Ping this endpoint to keep the free-tier service awake.
    return "ok", 200


@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("channel_post")
    if not message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()

    if message_id and already_seen(chat_id, message_id):
        return jsonify(ok=True)

    if not text:
        return jsonify(ok=True)

    if text in ("/start", "/help"):
        send_message(
            BOT_TOKEN,
            chat_id,
            "Send me a URL (Reddit, Twitter/X, Instagram, etc.) and I'll pull out the media and send it back.",
        )
        return jsonify(ok=True)

    if not text.startswith("http"):
        found = find_url(text)
        if not found:
            return jsonify(ok=True)
        text = found

    # Run locally in a real (killable) subprocess, not just a thread -- a
    # thread stuck in a slow video encode can't be forcibly stopped, but a
    # subprocess can. A watcher thread joins with a timeout; if local
    # processing is still running past that, it gets killed and the URL
    # falls back to GitHub automatically. No upfront guessing required.
    proc = multiprocessing.Process(target=process_url, args=(chat_id, text, message_id), daemon=True)
    proc.start()
    threading.Thread(target=_watch_and_fallback, args=(proc, text, chat_id, message_id), daemon=True).start()
    return jsonify(ok=True)


def process_url(chat_id, url, message_id):
    workdir = f"/tmp/media/{uuid.uuid4().hex}"
    try:
        items, status = extract_media(url, workdir)
        sendable = items[:MAX_ITEMS]

        sent = 0
        last_error = None
        for idx, item in enumerate(sendable):
            local_path = item["value"]
            is_file = item["source"] == "file"

            if not is_file:
                downloaded = _download_url_to_file(item["value"], workdir, idx)
                if downloaded:
                    local_path = downloaded
                    is_file = True
                else:
                    last_error = "Couldn't download media from source URL"
                    continue

            try:
                if item["kind"] == "photo":
                    resp = send_photo(BOT_TOKEN, chat_id, local_path, is_file=is_file)
                elif item["kind"] == "video":
                    resp = send_video(BOT_TOKEN, chat_id, local_path, is_file=is_file)
                else:
                    resp = send_document(BOT_TOKEN, chat_id, local_path, is_file=is_file)

                if resp.get("ok"):
                    sent += 1
                else:
                    last_error = resp.get("description", str(resp))
                    logger.warning("Telegram send failed: %s", resp)
            except Exception as e:
                last_error = str(e)
                logger.exception("Failed to send item: %s", item)

        if not items:
            dispatch_to_github(url, chat_id, message_id)
        elif sent == len(sendable) and sent > 0:
            # Everything found got sent successfully -- clean up the original message.
            if message_id:
                delete_message(BOT_TOKEN, chat_id, message_id)
        elif sent > 0:
            missed = len(sendable) - sent
            reason = f"\nReason: {last_error}" if last_error else ""
            send_message(
                BOT_TOKEN, chat_id,
                f"Sent {sent}/{len(sendable)} -- {missed} item(s) failed to send:\n{url}{reason}",
                reply_to_message_id=message_id,
            )
        else:
            dispatch_to_github(url, chat_id, message_id)

    except Exception as e:
        logger.exception("process_url failed")
        dispatch_to_github(url, chat_id, message_id)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
