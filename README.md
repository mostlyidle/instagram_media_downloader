# Instagram Media Downloader

A local Mac-ready Python CLI that accepts an Instagram profile URL, collects visible post/reel URLs with Playwright, and downloads media with yt-dlp. FFmpeg is used by yt-dlp when video streams need merging/remuxing.

This tool is intentionally limited to media that is publicly accessible or accessible through your own logged-in Instagram session. It does not bypass private accounts, paywalls, blocks, access controls, rate limits, or Instagram security measures. Use it only for profiles/media you have permission to download, and respect Instagram's terms and creators' rights.

## What it downloads

- Feed posts discovered from the profile grid
- Reels discovered from both the profile grid and the profile's Reels tab
- Carousel post media when yt-dlp can extract it
- Optional metadata JSON and thumbnails

It does **not** reliably download Stories, Highlights, disappearing content, private profiles you cannot normally access, or content hidden behind access controls.

## Install on macOS

From the project folder:

```bash
# 1) Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2) Install the project and its Python dependencies
python -m pip install --upgrade pip
pip install -e .

# 3) Install Playwright's Chromium browser
python -m playwright install chromium

# 4) Install FFmpeg if you do not already have it
brew install ffmpeg
```

## Optional: save a local Instagram login session

This opens a local browser window. You log in manually. The program saves cookies under `.auth/`, which is ignored by git.

```bash
ig-scraper login
```

Keep `.auth/instagram_cookies.txt` private. It can contain session cookies.

## Download a profile

```bash
ig-scraper --user USERNAME
```

`@USERNAME` works too. The username shortcut accepts the same download options
as the full command. Full profile URLs remain supported:

```bash
ig-scraper download "https://www.instagram.com/USERNAME/"
```

For testing, cap it to a small number of posts first:

```bash
ig-scraper --user USERNAME --max-posts 10 --show-browser --write-metadata
```

The downloaded media will be placed under:

```text
downloads/<extractor>/<username>/<media_id>.<ext>
```

The username is preferred; the numeric uploader ID is used only when yt-dlp
does not provide a username.

A manifest of collected URLs is written under:

```text
downloads/manifests/
```

Only one downloader run may execute from this project at a time. A second run
exits instead of sharing the Instagram session and generating additional traffic.

## Useful options

```bash
# Show browser while it scrolls the profile
--show-browser

# Increase/decrease scroll depth
--max-scrolls 120

# Slow down scrolling if Instagram is lazy-loading slowly
--scroll-pause 2.5

# Save yt-dlp metadata JSON files
--write-metadata

# Save thumbnails when available
--write-thumbnail

# Reuse an existing URL manifest without rescrolling the profile
--manifest downloads/manifests/USERNAME_YYYYMMDD_HHMMSS.json

# Stop after repeated access, missing-content, or rate-limit errors (default: 3)
--max-access-failures 3

# Download only still-image posts and image carousel slides
--images-only

# Also pass the profile URL directly to yt-dlp after collected post URLs
--also-try-profile-url

# Skip Playwright and give the profile URL directly to yt-dlp
--direct-only
```

### Rename existing numeric creator folders

Existing numeric folders are not renamed automatically. First preview verified
renames using metadata and saved manifests:

```bash
ig-scraper migrate-folders
```

Apply the preview only after checking it:

```bash
ig-scraper migrate-folders --apply
```

Folders with no unique verification, destination collisions, or symlinks are
left unchanged. For a folder that cannot be verified from local data, provide
an explicit mapping:

```bash
ig-scraper migrate-folders --map NUMERIC_ID=USERNAME --apply
```

## Troubleshooting

### It collected 0 URLs

Run:

```bash
ig-scraper login
ig-scraper --user USERNAME --show-browser
```

Then watch the browser window. If Instagram shows a login wall, challenge, captcha, rate-limit page, or private account page, the downloader will not bypass it.

### yt-dlp says login is required

Run the `login` command again, then rerun the download. Your cookies may have expired.

### Videos do not merge correctly

Install FFmpeg:

```bash
brew install ffmpeg
```

Then confirm it is available:

```bash
ffmpeg -version
```

### I want to resume a failed run

Rerun the same command. The tool uses `downloads/downloaded_archive.txt` so yt-dlp can skip media it already downloaded.

To avoid rescrolling the profile, reuse the manifest from the previous run:

```bash
ig-scraper --user USERNAME \
  --manifest downloads/manifests/USERNAME_YYYYMMDD_HHMMSS.json
```

### It stopped after access or rate-limit errors

The downloader stops after three consecutive HTTP 400/401/403/404/429, login, challenge, or captcha-style errors to avoid making more requests. A 404 can mean that a post was deleted or is no longer accessible. Wait before retrying; if Instagram asks you to sign in, run `login` again. You can resume with the saved manifest instead of collecting the profile again.
