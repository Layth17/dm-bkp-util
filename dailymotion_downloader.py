#!/usr/bin/env python3
"""
Dailymotion Channel Downloader
================================
Downloads all videos from a Dailymotion channel, organized by playlist.
Videos that don't belong to any playlist are saved in an "_Uncategorized" folder.

Requirements:
    pip install yt-dlp requests

Usage:
    python dailymotion_downloader.py --username YOUR_USERNAME [options]

Examples:
    python dailymotion_downloader.py --username johndoe
    python dailymotion_downloader.py --username johndoe --output ~/Downloads/my_channel
    python dailymotion_downloader.py --username johndoe --quality 1080
    python dailymotion_downloader.py --username johndoe --cookies cookies.txt
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("❌  yt-dlp not found. Install it with:  pip install yt-dlp")

try:
    import requests
except ImportError:
    sys.exit("❌  requests not found. Install it with:  pip install requests")


# ---------------------------------------------------------------------------
# Dailymotion API helpers
# ---------------------------------------------------------------------------

DM_API = "https://api.dailymotion.com"


def fetch_all_pages(url: str, params: dict) -> list:
    """Paginate through a Dailymotion API endpoint and return all items."""
    items = []
    page = 1
    while True:
        params["page"] = page
        params["limit"] = 100
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  ⚠  API error on page {page}: {exc}")
            break

        items.extend(data.get("list", []))
        if not data.get("has_more"):
            break
        page += 1
        time.sleep(0.3)   # be polite to the API
    return items


def get_channel_playlists(username: str) -> list[dict]:
    """Return all playlists for the given channel username."""
    print(f"📋  Fetching playlists for @{username} …")
    playlists = fetch_all_pages(
        f"{DM_API}/user/{username}/playlists",
        {"fields": "id,name,videos_total"},
    )
    print(f"    Found {len(playlists)} playlist(s).")
    return playlists


def get_all_channel_video_ids(username: str) -> set[str]:
    """Return the set of all video IDs uploaded by the channel."""
    print(f"🎞  Fetching full video list for @{username} …")
    videos = fetch_all_pages(
        f"{DM_API}/user/{username}/videos",
        {"fields": "id"},
    )
    ids = {v["id"] for v in videos}
    print(f"    Found {len(ids)} video(s) in total.")
    return ids


def get_playlist_video_ids(playlist_id: str) -> set[str]:
    """Return the set of video IDs inside a specific playlist."""
    videos = fetch_all_pages(
        f"{DM_API}/playlist/{playlist_id}/videos",
        {"fields": "id"},
    )
    return {v["id"] for v in videos}


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def build_ydl_opts(output_dir: Path, quality: int, cookies_file: str | None) -> dict:
    """Build yt-dlp options for a given output directory."""
    fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"

    opts: dict = {
        "format": fmt,
        "outtmpl": str(output_dir / "%(title)s [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "writeinfojson": True,
        "writesubtitles": True,
        "subtitleslangs": ["all"],
        "ignoreerrors": True,
        "noplaylist": True,      # we drive playlist logic ourselves
        "retries": 5,
        "fragment_retries": 5,
        "quiet": False,
        "no_warnings": False,
        "progress": True,
    }

    if cookies_file:
        opts["cookiefile"] = cookies_file

    return opts


def download_video(video_id: str, ydl_opts: dict) -> None:
    url = f"https://www.dailymotion.com/video/{video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def sanitize_folder_name(name: str) -> str:
    """Remove characters that are unsafe in directory names."""
    unsafe = r'\/:*?"<>|'
    for ch in unsafe:
        name = name.replace(ch, "_")
    return name.strip(". ") or "Untitled"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a Dailymotion channel organized by playlist."
    )
    parser.add_argument(
        "--username", "-u",
        required=True,
        help="Dailymotion username / channel handle (without @).",
    )
    parser.add_argument(
        "--output", "-o",
        default="./dailymotion_download",
        help="Root folder where videos will be saved. (default: ./dailymotion_download)",
    )
    parser.add_argument(
        "--quality", "-q",
        type=int,
        default=1080,
        help="Maximum video height in pixels, e.g. 720 or 1080. (default: 1080)",
    )
    parser.add_argument(
        "--cookies", "-c",
        default=None,
        help=(
            "Path to a Netscape-format cookies.txt file. "
            "Required if the channel has private or age-gated videos. "
            "Export from your browser with a 'Get cookies.txt' extension."
        ),
    )
    parser.add_argument(
        "--skip-uncategorized",
        action="store_true",
        help="Skip videos that do not belong to any playlist.",
    )
    args = parser.parse_args()

    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"\n📁  Saving to: {root}\n")

    # ------------------------------------------------------------------
    # 1. Discover playlists and all channel videos
    # ------------------------------------------------------------------
    playlists = get_channel_playlists(args.username)
    all_video_ids = get_all_channel_video_ids(args.username)

    # ------------------------------------------------------------------
    # 2. Download each playlist
    # ------------------------------------------------------------------
    categorized_ids: set[str] = set()

    for idx, pl in enumerate(playlists, 1):
        pl_id   = pl["id"]
        pl_name = sanitize_folder_name(pl.get("name") or f"Playlist_{pl_id}")
        pl_dir  = root / f"{idx:02d}_{pl_name}"
        pl_dir.mkdir(parents=True, exist_ok=True)

        video_ids = get_playlist_video_ids(pl_id)
        categorized_ids |= video_ids

        print(f"\n▶  [{idx}/{len(playlists)}] Playlist: '{pl_name}'  ({len(video_ids)} video(s))")
        print(f"   Folder: {pl_dir}")

        # Save a playlist manifest (handy for reference)
        manifest_path = pl_dir / "_playlist_info.json"
        manifest_path.write_text(
            json.dumps({"id": pl_id, "name": pl.get("name"), "video_ids": list(video_ids)}, indent=2)
        )

        ydl_opts = build_ydl_opts(pl_dir, args.quality, args.cookies)
        for vid_id in video_ids:
            print(f"  ⬇  {vid_id}")
            try:
                download_video(vid_id, ydl_opts)
            except Exception as exc:
                print(f"  ✗  Failed to download {vid_id}: {exc}")

    # ------------------------------------------------------------------
    # 3. Download uncategorized videos
    # ------------------------------------------------------------------
    uncategorized = all_video_ids - categorized_ids

    if uncategorized and not args.skip_uncategorized:
        unc_dir = root / "_Uncategorized"
        unc_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n▶  Uncategorized videos ({len(uncategorized)} video(s))")
        print(f"   Folder: {unc_dir}")

        ydl_opts = build_ydl_opts(unc_dir, args.quality, args.cookies)
        for vid_id in uncategorized:
            print(f"  ⬇  {vid_id}")
            try:
                download_video(vid_id, ydl_opts)
            except Exception as exc:
                print(f"  ✗  Failed to download {vid_id}: {exc}")
    elif uncategorized and args.skip_uncategorized:
        print(f"\nℹ  Skipping {len(uncategorized)} uncategorized video(s) (--skip-uncategorized).")

    # ------------------------------------------------------------------
    # 4. Summary
    # ------------------------------------------------------------------
    print(f"\n✅  All done!  Files saved in: {root}")


if __name__ == "__main__":
    main()