"""
Dailymotion Downloader — FastAPI Backend
"""

import asyncio
import json
import os
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

app = FastAPI(title="Dailymotion Downloader")

# ── Storage ──────────────────────────────────────────────────────────────────

DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# jobs: id → { status, logs, pause_event, stop_event, ... }
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


def get_playlist_video_ids(playlist_id: str) -> list[str]:
    videos = fetch_all_pages(
        f"{DM_API}/playlist/{playlist_id}/videos",
        {"fields": "id"},
    )
    return [v["id"] for v in videos]


def sanitize(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip(". ") or "Untitled"


# ── Custom exception for clean stop ──────────────────────────────────────────

class StopRequested(Exception):
    pass


# ── Download worker (runs in a thread) ───────────────────────────────────────

class ListLogger:
    """yt-dlp logger that appends messages to the job's log list."""
    def __init__(self, logs: list):
        self.logs = logs

    def debug(self, msg):
        if msg.startswith("[debug]") or msg.startswith("[download]"):
            return
        self.logs.append(msg)

    def info(self, msg):
        self.logs.append(msg)

    def warning(self, msg):
        self.logs.append(f"⚠  {msg}")

    def error(self, msg):
        self.logs.append(f"✗  {msg}")


def run_download(job_id: str, username: str, quality: str,
                 skip_uncategorized: bool, cookies_path: Optional[str],
                 selected_playlist_ids: Optional[list[str]] = None):
    job = jobs[job_id]
    logs: list = job["logs"]
    pause_event: threading.Event = job["pause_event"]
    stop_event: threading.Event  = job["stop_event"]

    def log(msg: str):
        logs.append(msg)

    def check_control():
        """Raise StopRequested if stopped; block if paused."""
        if stop_event.is_set():
            raise StopRequested()
        while pause_event.is_set():
            if stop_event.is_set():
                raise StopRequested()
            time.sleep(0.3)

    try:
        job["status"] = "running"
        root = Path(job["output_path"])
        root.mkdir(parents=True, exist_ok=True)

        archive_path = root / ".download_archive"
        resuming     = archive_path.exists()
        log(f"📂  Output directory: {root}")
        if resuming:
            log(f"♻️   Resuming — archive found, already-downloaded videos will be skipped.")
        else:
            log(f"🆕  Fresh download — starting from scratch.")

        fmt = "bestvideo+bestaudio/best" if quality == "best" \
              else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"

        def make_opts(out_dir: Path) -> dict:
            last_prog_time = [0.0]
            paused_logged  = [False]

            def progress_hook(d):
                if stop_event.is_set():
                    raise StopRequested()

                if pause_event.is_set():
                    if not paused_logged[0]:
                        logs.append("⏸  Paused — waiting…")
                        job["status"] = "paused"
                        paused_logged[0] = True
                    while pause_event.is_set():
                        if stop_event.is_set():
                            raise StopRequested()
                        time.sleep(0.3)
                    logs.append("▶  Resumed.")
                    job["status"] = "running"
                    paused_logged[0] = False

                if d["status"] == "downloading":
                    now = time.monotonic()
                    if now - last_prog_time[0] < 1.0:
                        return
                    last_prog_time[0] = now
                    downloaded = d.get("downloaded_bytes", 0)
                    total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                    pct = (downloaded / total * 100) if total else 0
                    speed = d.get("speed") or 0
                    eta   = d.get("eta")
                    speed_str = f"{speed / 1_048_576:.1f} MiB/s" if speed else "—"
                    eta_str   = f"{int(eta) // 60}:{int(eta) % 60:02d}" if eta else "—"
                    logs.append(f"__PROG__{pct:.1f}|{speed_str}|{eta_str}")
                elif d["status"] == "finished":
                    logs.append("__PROG__DONE__")

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
                "logger": ListLogger(logs),
                "progress_hooks": [progress_hook],
                "quiet": True,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                },
                "download_archive": str(archive_path),
            }
            if cookies_path:
                opts["cookiefile"] = cookies_path
            return opts

        # 1. Fetch playlists
        log(f"📋  Fetching playlists for @{username}…")
        playlists = get_playlists(username)

        if selected_playlist_ids:
            selected_set = set(selected_playlist_ids)
            playlists = [pl for pl in playlists if pl["id"] in selected_set]

        log(f"    Downloading {len(playlists)} playlist(s).")

        # 2. Fetch all video IDs
        log("🎞  Fetching full video list…")
        all_ids = get_all_video_ids(username)
        log(f"    Found {len(all_ids)} video(s) total.")

        categorized: set[str] = set()

        # 3. Download playlists
        for idx, pl in enumerate(playlists, 1):
            check_control()
            pl_id   = pl["id"]
            pl_name = sanitize(pl.get("name") or f"Playlist_{pl_id}")
            pl_dir  = root / f"{idx:02d}_{pl_name}"
            pl_dir.mkdir(parents=True, exist_ok=True)

            video_ids = get_playlist_video_ids(pl_id)
            categorized.update(video_ids)

            log(f"\n▶  [{idx}/{len(playlists)}] '{pl_name}'  ({len(video_ids)} video(s))")

            manifest = {"id": pl_id, "name": pl.get("name"), "video_ids": video_ids}
            (pl_dir / "_playlist_info.json").write_text(json.dumps(manifest, indent=2))

            with yt_dlp.YoutubeDL(make_opts(pl_dir)) as ydl:
                for v_idx, vid_id in enumerate(video_ids, 1):
                    check_control()
                    if v_idx > 1:
                        time.sleep(2)
                    log(f"  ⬇  [{v_idx}/{len(video_ids)}] {vid_id}")
                    try:
                        ydl.download([f"https://www.dailymotion.com/video/{vid_id}"])
                    except StopRequested:
                        raise
                    except Exception as exc:
                        log(f"  ✗  {vid_id}: {exc}")

        # 4. Uncategorized
        uncategorized = all_ids - categorized
        if uncategorized and not skip_uncategorized:
            unc_dir = root / "_Uncategorized"
            unc_dir.mkdir(parents=True, exist_ok=True)
            log(f"\n▶  Uncategorized ({len(uncategorized)} video(s))")
            with yt_dlp.YoutubeDL(make_opts(unc_dir)) as ydl:
                for v_idx, vid_id in enumerate(sorted(uncategorized), 1):
                    check_control()
                    if v_idx > 1:
                        time.sleep(2)
                    log(f"  ⬇  [{v_idx}/{len(uncategorized)}] {vid_id}")
                    try:
                        ydl.download([f"https://www.dailymotion.com/video/{vid_id}"])
                    except StopRequested:
                        raise
                    except Exception as exc:
                        log(f"  ✗  {vid_id}: {exc}")
        elif uncategorized and skip_uncategorized:
            log(f"\nℹ  Skipping {len(uncategorized)} uncategorized video(s).")

        log(f"\n✅  Done! Files saved to: {root}")
        job["status"] = "done"

    except StopRequested:
        log("\n⏹  Download stopped by user.")
        job["status"] = "stopped"
    except Exception as exc:
        log(f"\n❌  Fatal error: {exc}")
        job["status"] = "error"
    finally:
        if cookies_path and os.path.exists(cookies_path):
            os.unlink(cookies_path)
        logs.append(None)  # sentinel — stream finished


# ── API routes ────────────────────────────────────────────────────────────────

def make_job_id(username: str) -> str:
    slug = username.strip().lower()
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{slug}_{ts}"


@app.get("/api/ipinfo")
async def ipinfo():
    try:
        resp = requests.get("https://ipinfo.io/json", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/channel/playlists")
async def channel_playlists(username: str):
    try:
        return get_playlists(username)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/start")
async def start_download(
    background_tasks: BackgroundTasks,
    username: str  = Form(...),
    quality: str   = Form("best"),
    skip_uncategorized: bool = Form(False),
    subfolder: str = Form(""),
    playlist_ids: str = Form(""),
    cookies: Optional[UploadFile] = File(None),
):
    job_id = make_job_id(username)
    cookies_path = None

    if cookies and cookies.filename:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmp.write(await cookies.read())
        tmp.close()
        cookies_path = tmp.name

    selected_ids: Optional[list[str]] = None
    if playlist_ids.strip():
        try:
            parsed = json.loads(playlist_ids)
            if parsed:  # empty list = download all
                selected_ids = parsed
        except Exception:
            pass

    sub      = subfolder.strip().strip("/")
    slug     = re.sub(r"[^\w\-]", "_", username.strip().lower())
    out_root = DOWNLOAD_ROOT / sub / slug if sub else DOWNLOAD_ROOT / slug

    jobs[job_id] = {
        "id": job_id,
        "username": username,
        "status": "pending",
        "logs": [],
        "pause_event": threading.Event(),
        "stop_event":  threading.Event(),
        "output_path": str(out_root),
    }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, username, quality, skip_uncategorized, cookies_path, selected_ids),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    job = jobs[job_id]
    if job["status"] == "running":
        job["pause_event"].set()
        job["status"] = "paused"
    return {"status": job["status"]}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    job = jobs[job_id]
    if job["status"] == "paused":
        job["pause_event"].clear()
        job["status"] = "running"
    return {"status": job["status"]}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    job = jobs[job_id]
    job["pause_event"].clear()   # unblock if paused so thread can exit
    job["stop_event"].set()
    return {"status": "stopping"}


@app.get("/api/jobs/{job_id}/logs")
async def stream_logs(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    job_logs: list = jobs[job_id]["logs"]

    async def event_generator():
        pos = 0
        while True:
            if pos < len(job_logs):
                msg = job_logs[pos]
                pos += 1
                if msg is None:
                    yield "data: __DONE__\n\n"
                    break
                safe = msg.replace("\n", "↵")
                yield f"data: {safe}\n\n"
            else:
                yield "data: __PING__\n\n"
                await asyncio.sleep(0.3)

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
