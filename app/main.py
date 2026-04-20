"""
Dailymotion Downloader — FastAPI Backend
"""

import asyncio
import json
import os
import queue
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Dailymotion Downloader")

# ── Storage ──────────────────────────────────────────────────────────────────

DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# jobs: id → { status, log_queue, meta }
jobs: dict[str, dict] = {}

DM_API = "https://api.dailymotion.com"


# ── Dailymotion API helpers ───────────────────────────────────────────────────

def fetch_all_pages(url: str, params: dict) -> list:
    items = []
    page = 1
    while True:
        params["page"] = page
        params["limit"] = 100
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("list", []))
        if not data.get("has_more"):
            break
        page += 1
        time.sleep(0.3)
    return items


def get_playlists(username: str) -> list[dict]:
    return fetch_all_pages(
        f"{DM_API}/user/{username}/playlists",
        {"fields": "id,name,videos_total"},
    )


def get_all_video_ids(username: str) -> set[str]:
    videos = fetch_all_pages(
        f"{DM_API}/user/{username}/videos",
        {"fields": "id"},
    )
    return {v["id"] for v in videos}


def get_playlist_video_ids(playlist_id: str) -> set[str]:
    videos = fetch_all_pages(
        f"{DM_API}/playlist/{playlist_id}/videos",
        {"fields": "id"},
    )
    return {v["id"] for v in videos}


def sanitize(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip(". ") or "Untitled"


# ── Download worker (runs in a thread) ───────────────────────────────────────

class QueueLogger:
    """yt-dlp logger that forwards messages to a thread-safe queue."""
    def __init__(self, q: queue.Queue):
        self.q = q

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        self.q.put(msg)

    def info(self, msg):
        self.q.put(msg)

    def warning(self, msg):
        self.q.put(f"⚠  {msg}")

    def error(self, msg):
        self.q.put(f"✗  {msg}")


def run_download(job_id: str, username: str, quality: str,
                 skip_uncategorized: bool, cookies_path: Optional[str]):
    job = jobs[job_id]
    log_q: queue.Queue = job["log_queue"]

    def log(msg: str):
        log_q.put(msg)

    try:
        job["status"] = "running"
        root = Path(job["output_path"])
        root.mkdir(parents=True, exist_ok=True)

        fmt = "bestvideo+bestaudio/best" if quality == "best" \
              else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"

        def make_opts(out_dir: Path) -> dict:
            opts = {
                "format": fmt,
                "outtmpl": str(out_dir / "%(title)s [%(id)s].%(ext)s"),
                "merge_output_format": "mp4",
                "writethumbnail": True,
                "writeinfojson": True,
                "writesubtitles": True,
                "subtitleslangs": ["all"],
                "ignoreerrors": True,
                "noplaylist": True,
                "retries": 5,
                "fragment_retries": 5,
                "logger": QueueLogger(log_q),
                "quiet": True,
            }
            if cookies_path:
                opts["cookiefile"] = cookies_path
            return opts

        # 1. Fetch playlists
        log(f"📋  Fetching playlists for @{username}…")
        playlists = get_playlists(username)
        log(f"    Found {len(playlists)} playlist(s).")

        # 2. Fetch all video IDs
        log(f"🎞  Fetching full video list…")
        all_ids = get_all_video_ids(username)
        log(f"    Found {len(all_ids)} video(s) total.")

        categorized: set[str] = set()

        # 3. Download playlists
        for idx, pl in enumerate(playlists, 1):
            pl_id   = pl["id"]
            pl_name = sanitize(pl.get("name") or f"Playlist_{pl_id}")
            pl_dir  = root / f"{idx:02d}_{pl_name}"
            pl_dir.mkdir(parents=True, exist_ok=True)

            video_ids = get_playlist_video_ids(pl_id)
            categorized |= video_ids

            log(f"\n▶  [{idx}/{len(playlists)}] '{pl_name}'  ({len(video_ids)} video(s))")

            manifest = {"id": pl_id, "name": pl.get("name"), "video_ids": list(video_ids)}
            (pl_dir / "_playlist_info.json").write_text(json.dumps(manifest, indent=2))

            with yt_dlp.YoutubeDL(make_opts(pl_dir)) as ydl:
                for vid_id in video_ids:
                    log(f"  ⬇  {vid_id}")
                    try:
                        ydl.download([f"https://www.dailymotion.com/video/{vid_id}"])
                    except Exception as exc:
                        log(f"  ✗  {vid_id}: {exc}")

        # 4. Uncategorized
        uncategorized = all_ids - categorized
        if uncategorized and not skip_uncategorized:
            unc_dir = root / "_Uncategorized"
            unc_dir.mkdir(parents=True, exist_ok=True)
            log(f"\n▶  Uncategorized ({len(uncategorized)} video(s))")
            with yt_dlp.YoutubeDL(make_opts(unc_dir)) as ydl:
                for vid_id in uncategorized:
                    log(f"  ⬇  {vid_id}")
                    try:
                        ydl.download([f"https://www.dailymotion.com/video/{vid_id}"])
                    except Exception as exc:
                        log(f"  ✗  {vid_id}: {exc}")
        elif uncategorized and skip_uncategorized:
            log(f"\nℹ  Skipping {len(uncategorized)} uncategorized video(s).")

        log(f"\n✅  Done! Files saved to: {root}")
        job["status"] = "done"

    except Exception as exc:
        log(f"\n❌  Fatal error: {exc}")
        job["status"] = "error"
    finally:
        if cookies_path and os.path.exists(cookies_path):
            os.unlink(cookies_path)
        log_q.put(None)  # sentinel


# ── API routes ────────────────────────────────────────────────────────────────

def make_job_id(username: str) -> str:
    """e.g.  johndoe_20250419_143022"""
    slug = re.sub(r"[^\w\-]", "_", username.strip().lower())
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{slug}_{ts}"


@app.post("/api/start")
async def start_download(
    background_tasks: BackgroundTasks,
    username: str  = Form(...),
    quality: str   = Form("best"),
    skip_uncategorized: bool = Form(False),
    subfolder: str = Form(""),
    cookies: Optional[UploadFile] = File(None),
):
    job_id = make_job_id(username)
    cookies_path = None

    if cookies and cookies.filename:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmp.write(await cookies.read())
        tmp.close()
        cookies_path = tmp.name

    # Resolve output: /downloads/{subfolder}/{job_id}  or  /downloads/{job_id}
    sub      = subfolder.strip().strip("/")
    out_root = DOWNLOAD_ROOT / sub / job_id if sub else DOWNLOAD_ROOT / job_id

    jobs[job_id] = {
        "id": job_id,
        "username": username,
        "status": "pending",
        "log_queue": queue.Queue(),
        "output_path": str(out_root),
    }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, username, quality, skip_uncategorized, cookies_path),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/logs")
async def stream_logs(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    log_q: queue.Queue = jobs[job_id]["log_queue"]

    async def event_generator():
        while True:
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: log_q.get(timeout=1)
                )
                if msg is None:   # sentinel — stream finished
                    yield f"data: __DONE__\n\n"
                    break
                safe = msg.replace("\n", "↵")
                yield f"data: {safe}\n\n"
            except queue.Empty:
                yield f"data: __PING__\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    j = jobs[job_id]
    return {"id": j["id"], "status": j["status"],
            "username": j["username"], "output_path": j.get("output_path")}


@app.get("/api/jobs")
async def list_jobs():
    return [
        {"id": j["id"], "status": j["status"], "username": j["username"]}
        for j in jobs.values()
    ]


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    html = (Path(__file__).parent / "static" / "index.html").read_text()
    return HTMLResponse(html)