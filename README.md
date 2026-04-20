Disclosure: This project is largely vibe coded.

# DM Retriever — Setup & Usage

A local web UI for downloading a Dailymotion channel's content, organized by playlist. Supports resuming interrupted downloads, parallel jobs, and pause/stop/resume controls.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose (included with Docker Desktop)

## Quick Start

```bash
# 1. Build and start the container
docker compose up --build

# 2. Open your browser
open http://localhost:8946
```

## Features

- **Playlist selection** — indexes the channel and lets you choose which playlists to download (all selected by default)
- **Resume support** — re-running a job for the same username skips already-downloaded videos instantly via a `.download_archive` file; no re-downloading
- **Multiple concurrent jobs** — start as many jobs as you want simultaneously
- **Pause / Resume / Stop** — per-job controls available in the Active Jobs panel
- **Live log viewer** — click any job (active or finished) to view its full output log
- **Progress bar** — per-video download progress shown inline in the log
- **Bot mitigation** — uses a browser User-Agent and a short delay between downloads to reduce 401 errors from Dailymotion
- **Public IP display** — shows the container's egress IP, location, and ISP in the header so you know what IP is doing the downloading
- **Cookies support** — upload a `cookies.txt` for private or age-restricted videos

## Download Structure

Downloads are organized under `./downloads/` by username, then playlist:

```
downloads/
└── jon.doe/
    ├── .download_archive        ← tracks completed videos for resume
    ├── 01_My Playlist/
    │   ├── _playlist_info.json
    │   ├── My Video Title [xyzabc].mp4
    │   └── …
    ├── 02_Another Playlist/
    └── _Uncategorized/
```

Re-running a job for the same username writes into the same folder and skips anything already in the archive.

## Resuming an Interrupted Download

Just start a new job with the same username (and same subfolder if you used one). The app will detect the existing `.download_archive` and skip completed videos automatically.

## Changing the Port

Edit `docker-compose.yml` and change the left-hand port number:

```yaml
ports:
  - "9000:8946"   # now accessible at http://localhost:9000
```

## Stopping the Container

```bash
docker compose down
```

## Private / Age-Restricted Videos

Export your browser cookies while logged into Dailymotion as a `cookies.txt` file (Netscape format) using an extension like **"Get cookies.txt LOCALLY"** (Chrome/Firefox), then upload the file in the UI before starting the job.