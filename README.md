# DM Retriever — Setup & Usage

A local web UI for downloading your Dailymotion channel content, organized by playlist.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose (included with Docker Desktop)

## Quick Start

```bash
# 1. Build and start the container
docker compose up --build

# 2. Open your browser
open http://localhost:8080
```

Downloaded videos appear in the `./downloads/` folder next to this file,
organized as:

```
downloads/
└── <job-id>/
    ├── 01_My Playlist/
    │   ├── _playlist_info.json
    │   ├── My Video Title [xyzabc].mp4
    │   └── …
    ├── 02_Another Playlist/
    └── _Uncategorized/
```

## Changing the Port

Edit `docker-compose.yml` and change the left-hand port number:

```yaml
ports:
  - "9000:8080"   # now accessible at http://localhost:9000
```

## Stopping

```bash
docker compose down
```

## Private Videos (Cookies)

If some videos are private or age-gated, export your browser cookies while
logged into Dailymotion as a `cookies.txt` file (Netscape format) using an
extension like **"Get cookies.txt LOCALLY"**, then upload the file in the UI.