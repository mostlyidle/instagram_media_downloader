from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, determine_ext

INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
# Instagram can expose a media link either directly (``/reel/SHORTCODE/``)
# or under the profile that owns it (``/username/reel/SHORTCODE/``).
# ``/reels/`` is also used by some current Instagram pages.
POST_PATH_RE = re.compile(r"^/(?:[^/?#]+/)?(p|reels?|tv)/([^/?#]+)/?", re.IGNORECASE)
PROFILE_EXCLUDED_PREFIXES = {
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "explore",
    "accounts",
    "direct",
    "about",
    "developer",
    "legal",
}


@dataclass(frozen=True)
class DownloadPlan:
    profile_url: str
    urls: list[str]
    manifest_path: Path
    output_dir: Path


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def normalize_instagram_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("URL is empty")
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    parsed = urlparse(raw_url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "instagram.com":
        raise ValueError("Expected an instagram.com profile URL")

    clean_path = parsed.path.rstrip("/") + "/"
    return urlunparse(("https", "www.instagram.com", clean_path, "", "", ""))


def is_profile_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return len(parts) == 1 and parts[0].lower() not in PROFILE_EXCLUDED_PREFIXES


def extract_post_urls_from_hrefs(hrefs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []

    for href in hrefs:
        if not href:
            continue
        parsed = urlparse(href)
        path = parsed.path if parsed.scheme else urlparse("https://www.instagram.com" + href).path
        match = POST_PATH_RE.match(path)
        if not match:
            continue
        kind, shortcode = match.group(1).lower(), match.group(2)
        if kind == "reels":
            kind = "reel"
        canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
        if canonical not in seen:
            seen.add(canonical)
            urls.append(canonical)
    return urls


def maybe_click_button_by_text(page, texts: Iterable[str]) -> None:
    for text in texts:
        try:
            locator = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
            if locator.count() > 0:
                locator.first.click(timeout=1500)
                time.sleep(0.3)
        except Exception:
            pass


def wait_for_profile_media_links(page, timeout_ms: int = 20_000) -> bool:
    """Wait until Instagram has rendered at least one post or reel card."""
    try:
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll("a[href]")).some(
                link => /^\\/(?:[^/?#]+\\/)?(?:p|reels?|tv)\\/[^/?#]+\\/?$/i.test(
                    new URL(link.href).pathname
                )
            )
            """,
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def export_cookies_to_netscape(cookies: list[dict], cookie_file: Path) -> None:
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated locally by instagram_media_downloader.",
        "# Keep this file private; it can contain authenticated session cookies.",
    ]
    for c in cookies:
        domain = c.get("domain", "")
        if "instagram.com" not in domain:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires") or 0
        try:
            expires = int(expires)
        except (TypeError, ValueError):
            expires = 0
        if expires < 0:
            expires = 0
        name = c.get("name", "")
        value = c.get("value", "")
        if name:
            lines.append("\t".join([domain, include_subdomains, path, secure, str(expires), name, value]))
    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def login(storage_state: Path, cookie_file: Path, channel: str | None = None) -> None:
    storage_state.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel=channel)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        print("A browser window opened. Log in to Instagram manually.")
        print("After the Instagram home/feed page loads, return here and press Enter.")
        input()
        context.storage_state(path=str(storage_state))
        export_cookies_to_netscape(context.cookies(), cookie_file)
        browser.close()

    print(f"Saved Playwright session: {storage_state}")
    print(f"Saved yt-dlp cookie file: {cookie_file}")


def collect_profile_post_urls(
    profile_url: str,
    storage_state: Path | None,
    cookie_file: Path | None,
    headless: bool,
    max_scrolls: int,
    scroll_pause: float,
    max_posts: int | None,
    channel: str | None = None,
) -> list[str]:
    if not is_profile_url(profile_url):
        raise ValueError("Expected a profile URL like https://www.instagram.com/username/")

    found: dict[str, None] = {}
    last_count = 0
    idle_rounds = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel=channel)
        context_kwargs = {}
        if storage_state and storage_state.exists():
            context_kwargs["storage_state"] = str(storage_state)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)

        maybe_click_button_by_text(
            page,
            [
                "allow all cookies",
                "accept all",
                "decline optional cookies",
                "not now",
                "close",
            ],
        )

        if not wait_for_profile_media_links(page):
            eprint(
                "No post/reel links appeared during the initial 20-second wait "
                f"(page URL: {page.url}; anchors: {page.locator('a[href]').count()})."
            )

        for scroll_num in range(max_scrolls + 1):
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
            for url in extract_post_urls_from_hrefs(hrefs):
                found.setdefault(url, None)
                if max_posts and len(found) >= max_posts:
                    break

            print(f"Collected {len(found)} post/reel URLs after scroll {scroll_num}/{max_scrolls}")

            if max_posts and len(found) >= max_posts:
                break

            if len(found) == last_count:
                idle_rounds += 1
            else:
                idle_rounds = 0
                last_count = len(found)

            if idle_rounds >= 4 and scroll_num >= 4:
                print("No new URLs found after several scrolls; stopping collection.")
                break

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(scroll_pause)

        if storage_state:
            storage_state.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(storage_state))
        if cookie_file:
            export_cookies_to_netscape(context.cookies(), cookie_file)
        browser.close()

    return list(found.keys())


def build_ydl_options(
    output_dir: Path,
    archive_path: Path,
    cookie_file: Path | None,
    write_metadata: bool,
    write_thumbnail: bool,
    images_only: bool = False,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(output_dir / "%(extractor)s" / "%(uploader_id|unknown)s" / "%(id)s.%(ext)s"),
        "download_archive": str(archive_path),
        "ignoreerrors": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "sleep_interval_requests": 1,
        "sleep_interval": 3,
        "max_sleep_interval": 7,
        "noplaylist": False,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 1,
        "quiet": False,
        "no_warnings": False,
    }
    if cookie_file and cookie_file.exists():
        opts["cookiefile"] = str(cookie_file)
    if write_metadata:
        opts["writeinfojson"] = True
    if write_thumbnail and not images_only:
        opts["writethumbnail"] = True
    if images_only:
        # Instagram exposes stills as thumbnails rather than video formats.
        # Keep their metadata available when inspecting a post without video.
        opts["ignore_no_formats_error"] = True
    return opts


def iter_media_entries(info: dict | None) -> Iterable[dict]:
    """Yield individual post or carousel media entries from yt-dlp metadata."""
    if not info:
        return
    if info.get("_type") in {"playlist", "multi_video"}:
        for entry in info.get("entries") or []:
            if isinstance(entry, dict):
                yield from iter_media_entries(entry)
        return
    yield info


def build_image_download_info(info: dict) -> dict | None:
    """Turn Instagram's best image candidate into a yt-dlp downloadable item."""
    image_url = info.get("thumbnail")
    if not image_url:
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            image_url = thumbnails[-1].get("url")
    if not image_url:
        return None

    image_info = dict(info)
    image_info.pop("formats", None)
    image_info.pop("requested_formats", None)
    image_info.setdefault("extractor", "instagram")
    image_info.setdefault("extractor_key", "Instagram")
    image_info.update(
        {
            "title": info.get("title") or f"Image {info.get('id', 'unknown')}",
            "url": image_url,
            "ext": determine_ext(image_url, "jpg"),
            "format_id": "image",
            "format": "image",
            "protocol": urlparse(image_url).scheme,
            "vcodec": "none",
            "acodec": "none",
        }
    )
    return image_info


def download_images(
    urls: list[str],
    output_dir: Path,
    archive_path: Path,
    cookie_file: Path | None,
    write_metadata: bool,
) -> None:
    opts = build_ydl_options(
        output_dir=output_dir,
        archive_path=archive_path,
        cookie_file=cookie_file,
        write_metadata=write_metadata,
        write_thumbnail=False,
        images_only=True,
    )
    image_count = 0

    with YoutubeDL(opts) as ydl:
        for url in urls:
            try:
                info = ydl.extract_info(url, download=False)
            except DownloadError as exc:
                eprint(f"Could not inspect {url}: {exc}")
                continue
            if not info:
                # yt-dlp returns no metadata when this URL is already in the
                # download archive. There is nothing new to inspect here.
                continue

            for entry in iter_media_entries(info):
                # Video entries have downloadable formats. Image entries have
                # only image candidates in their thumbnails collection.
                if entry.get("formats"):
                    continue
                image_info = build_image_download_info(entry)
                if not image_info:
                    continue
                ydl.process_info(image_info)
                image_count += 1

    print(f"Processed {image_count} image(s).")


def download_urls(
    urls: list[str],
    output_dir: Path,
    archive_path: Path,
    cookie_file: Path | None,
    write_metadata: bool,
    write_thumbnail: bool,
    images_only: bool,
) -> None:
    if not urls:
        print("No URLs to download.")
        return

    if images_only:
        download_images(
            urls=urls,
            output_dir=output_dir,
            archive_path=archive_path,
            cookie_file=cookie_file,
            write_metadata=write_metadata,
        )
        return

    if not shutil.which("ffmpeg"):
        eprint("Warning: ffmpeg was not found on PATH. Videos may not merge/remux correctly.")

    opts = build_ydl_options(
        output_dir=output_dir,
        archive_path=archive_path,
        cookie_file=cookie_file,
        write_metadata=write_metadata,
        write_thumbnail=write_thumbnail,
        images_only=False,
    )
    with YoutubeDL(opts) as ydl:
        ydl.download(urls)


def make_plan(args: argparse.Namespace) -> DownloadPlan:
    profile_url = normalize_instagram_url(args.profile_url)
    output_dir = Path(args.output).expanduser().resolve()
    manifest_dir = output_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    username = [p for p in urlparse(profile_url).path.split("/") if p][0]
    manifest_path = manifest_dir / f"{username}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    return DownloadPlan(profile_url=profile_url, urls=[], manifest_path=manifest_path, output_dir=output_dir)


def command_download(args: argparse.Namespace) -> None:
    plan = make_plan(args)
    storage_state = Path(args.storage_state).expanduser().resolve() if args.storage_state else None
    cookie_file = Path(args.cookies).expanduser().resolve() if args.cookies else None
    archive_path = plan.output_dir / "downloaded_archive.txt"

    if args.direct_only:
        urls = [plan.profile_url]
    else:
        urls = collect_profile_post_urls(
            profile_url=plan.profile_url,
            storage_state=storage_state,
            cookie_file=cookie_file,
            headless=not args.show_browser,
            max_scrolls=args.max_scrolls,
            scroll_pause=args.scroll_pause,
            max_posts=args.max_posts,
            channel=args.browser_channel,
        )
        if args.also_try_profile_url:
            urls.append(plan.profile_url)
        # De-duplicate after optional profile URL append.
        urls = list(dict.fromkeys(urls))

    manifest = {
        "profile_url": plan.profile_url,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "count": len(urls),
        "urls": urls,
    }
    plan.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote manifest: {plan.manifest_path}")

    if not urls:
        print("No post/reel URLs were collected. Try running `login`, then rerun with --show-browser.")
        return

    download_urls(
        urls=urls,
        output_dir=plan.output_dir,
        archive_path=archive_path,
        cookie_file=cookie_file,
        write_metadata=args.write_metadata,
        write_thumbnail=args.write_thumbnail,
        images_only=args.images_only,
    )
    print(f"Done. Downloads are under: {plan.output_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ig-media-downloader",
        description="Download media from Instagram profiles that are publicly accessible or accessible to your own logged-in session.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="Open a browser so you can log in manually and save a local session.")
    login_parser.add_argument("--storage-state", default=".auth/instagram_state.json")
    login_parser.add_argument("--cookies", default=".auth/instagram_cookies.txt")
    login_parser.add_argument(
        "--browser-channel",
        default=None,
        help="Optional Playwright browser channel, e.g. chrome. Leave empty to use Playwright Chromium.",
    )
    login_parser.set_defaults(func=lambda args: login(
        storage_state=Path(args.storage_state).expanduser().resolve(),
        cookie_file=Path(args.cookies).expanduser().resolve(),
        channel=args.browser_channel,
    ))

    dl = subparsers.add_parser("download", help="Collect profile post/reel URLs and download media with yt-dlp.")
    dl.add_argument("profile_url", help="Instagram profile URL, e.g. https://www.instagram.com/natgeo/")
    dl.add_argument("--output", default="downloads", help="Output folder. Default: downloads")
    dl.add_argument("--storage-state", default=".auth/instagram_state.json", help="Playwright storage state from login command.")
    dl.add_argument("--cookies", default=".auth/instagram_cookies.txt", help="Netscape cookies file for yt-dlp.")
    dl.add_argument("--max-scrolls", type=int, default=80, help="Maximum profile scrolls while collecting links.")
    dl.add_argument("--scroll-pause", type=float, default=1.5, help="Seconds to wait after each scroll.")
    dl.add_argument("--max-posts", type=int, default=None, help="Optional cap for testing, e.g. --max-posts 10")
    dl.add_argument("--show-browser", action="store_true", help="Run Playwright visibly instead of headless.")
    dl.add_argument("--browser-channel", default=None, help="Optional Playwright browser channel, e.g. chrome.")
    dl.add_argument("--write-metadata", action="store_true", help="Save yt-dlp .info.json files.")
    dl.add_argument("--write-thumbnail", action="store_true", help="Save thumbnails when yt-dlp exposes them.")
    dl.add_argument(
        "--images-only",
        action="store_true",
        help="Download still-image posts and image carousel slides, skipping videos and Reels.",
    )
    dl.add_argument("--also-try-profile-url", action="store_true", help="Also pass the profile URL directly to yt-dlp after collected URLs.")
    dl.add_argument("--direct-only", action="store_true", help="Skip Playwright collection and pass the profile URL directly to yt-dlp.")
    dl.set_defaults(func=command_download)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit("Interrupted.")
    except Exception as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
