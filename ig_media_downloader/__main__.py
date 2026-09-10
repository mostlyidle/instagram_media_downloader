from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, determine_ext

INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
# Instagram can expose a media link either directly (``/reel/SHORTCODE/``)
# or under the profile that owns it (``/username/reel/SHORTCODE/``).
# ``/reels/`` is also used by some current Instagram pages.
POST_PATH_RE = re.compile(r"^/(?:[^/?#]+/)?(p|reels?(?!/audio/)|tv)/([^/?#]+)/?", re.IGNORECASE)
INSTAGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
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

# Keep collection state in the page so each scroll only transfers newly rendered
# media links to Python instead of reparsing every visible profile anchor.
COLLECT_NEW_MEDIA_HREFS_JS = r"""
els => {
    const seen = window.__igMediaDownloaderSeen || new Set();
    window.__igMediaDownloaderSeen = seen;
    const hrefs = [];

    for (const link of els) {
        const match = /^\/(?:[^/?#]+\/)?(p|reels?(?!\/audio\/)|tv)\/([^/?#]+)\/?/i.exec(
            link.pathname
        );
        if (!match) continue;

        let kind = match[1].toLowerCase();
        if (kind === "reels") kind = "reel";
        const key = match[2];
        if (seen.has(key)) continue;

        seen.add(key);
        hrefs.push(link.href);
    }
    return hrefs;
}
"""

HAS_NEW_MEDIA_HREF_JS = r"""
() => {
    const seen = window.__igMediaDownloaderSeen || new Set();
    return Array.from(document.querySelectorAll("a[href]")).some(link => {
        const match = /^\/(?:[^/?#]+\/)?(p|reels?(?!\/audio\/)|tv)\/([^/?#]+)\/?/i.exec(
            link.pathname
        );
        if (!match) return false;

        let kind = match[1].toLowerCase();
        if (kind === "reels") kind = "reel";
        return !seen.has(match[2]);
    });
}
"""

ACCESS_ERROR_MARKERS = (
    "http error 400",
    "http error 401",
    "http error 403",
    "http error 404",
    "http error 429",
    "rate limit",
    "rate-limit",
    "login required",
    "login is required",
    "challenge",
    "captcha",
    "empty media response",
)


@dataclass(frozen=True)
class DownloadPlan:
    profile_url: str
    urls: list[str]
    manifest_path: Path
    output_dir: Path


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


class AccessCircuitBreaker:
    """Stop a run before repeated access failures generate more traffic."""

    def __init__(self, max_failures: int) -> None:
        if max_failures < 1:
            raise ValueError("max_access_failures must be at least 1")
        self.max_failures = max_failures
        self.consecutive_failures = 0

    def record(self, url: str, errors: Iterable[str], succeeded: bool) -> None:
        access_errors = [error for error in errors if self._is_access_error(error)]
        if access_errors:
            self.consecutive_failures += 1
            eprint(
                f"Access/content/rate-limit error {self.consecutive_failures}/{self.max_failures} for {url}."
            )
            if self.consecutive_failures >= self.max_failures:
                raise RuntimeError(
                    "Stopped after repeated access, missing-content, or rate-limit errors. Wait before retrying, "
                    "then run `login` if Instagram asks you to sign in again."
                )
        elif succeeded:
            self.consecutive_failures = 0

    @staticmethod
    def _is_access_error(error: str) -> bool:
        message = error.lower()
        return any(marker in message for marker in ACCESS_ERROR_MARKERS)


class DownloadLogger:
    """Keep yt-dlp output visible while retaining errors for the circuit breaker."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        if not message.startswith("[debug]"):
            print(message)

    def info(self, message: str) -> None:
        print(message)

    def warning(self, message: str) -> None:
        eprint(f"WARNING: {message}")

    def error(self, message: str) -> None:
        self.errors.append(message)
        eprint(f"ERROR: {message}")


@contextmanager
def workspace_run_lock(lock_path: Path) -> Iterator[None]:
    """Prevent concurrent runs in one workspace from sharing an Instagram session."""
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RuntimeError("Another downloader run is already active in this workspace") from exc

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


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


def profile_url_from_username(raw_username: str) -> str:
    """Build a canonical Instagram profile URL from a username."""
    username = raw_username.strip().removeprefix("@")
    if not INSTAGRAM_USERNAME_RE.fullmatch(username):
        raise ValueError("--user must be a valid Instagram username")
    return f"https://www.instagram.com/{username}/"


def expand_user_shortcut(argv: list[str]) -> list[str]:
    """Translate ``--user USERNAME`` into the existing download command."""
    if "--user" not in argv:
        return argv
    if not argv or argv[0] != "--user":
        raise ValueError("Use --user before any download options")
    if len(argv) < 2:
        raise ValueError("--user requires an Instagram username")
    return ["download", profile_url_from_username(argv[1]), *argv[2:]]


def is_profile_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return len(parts) == 1 and parts[0].lower() not in PROFILE_EXCLUDED_PREFIXES


def load_manifest_urls(manifest_path: Path, expected_profile_url: str) -> list[str]:
    """Load and validate URLs collected by an earlier run for this profile."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Manifest was not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Manifest is not valid JSON: {manifest_path}") from exc

    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest must contain an object: {manifest_path}")
    profile_url = manifest.get("profile_url")
    if not isinstance(profile_url, str):
        raise ValueError(f"Manifest has no profile_url: {manifest_path}")
    if normalize_instagram_url(profile_url) != expected_profile_url:
        raise ValueError("Manifest profile_url does not match the requested profile URL")

    manifest_urls = manifest.get("urls")
    if not isinstance(manifest_urls, list) or not all(isinstance(url, str) for url in manifest_urls):
        raise ValueError(f"Manifest has no valid URL list: {manifest_path}")

    urls = extract_post_urls_from_hrefs(manifest_urls)
    if len(urls) != len(manifest_urls):
        raise ValueError(f"Manifest contains an unsupported media URL: {manifest_path}")
    return urls


def safe_creator_folder_name(value: object) -> str | None:
    """Return a username safe to use as one output-directory component."""
    if not isinstance(value, str) or value in {".", ".."}:
        return None
    if not INSTAGRAM_USERNAME_RE.fullmatch(value):
        return None
    return value


def profile_username(profile_url: str) -> str | None:
    try:
        normalized = normalize_instagram_url(profile_url)
    except ValueError:
        return None
    parts = [part for part in urlparse(normalized).path.split("/") if part]
    if len(parts) != 1 or parts[0].lower() in PROFILE_EXCLUDED_PREFIXES:
        return None
    return safe_creator_folder_name(parts[0])


def _manifest_creator_candidates(manifest_dir: Path, media_ids: set[str]) -> set[str]:
    candidates: set[str] = set()
    if not manifest_dir.is_dir():
        return candidates

    for manifest_path in manifest_dir.glob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        username = profile_username(manifest.get("profile_url", ""))
        manifest_urls = manifest.get("urls")
        if not username or not isinstance(manifest_urls, list):
            continue
        urls = extract_post_urls_from_hrefs(url for url in manifest_urls if isinstance(url, str))
        if any(urlparse(url).path.rstrip("/").split("/")[-1] in media_ids for url in urls):
            candidates.add(username)
    return candidates


def _metadata_creator_candidates(folder: Path, folder_id: str) -> set[str]:
    candidates: set[str] = set()
    for metadata_path in folder.glob("*.info.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        uploader_id = str(metadata.get("uploader_id", ""))
        username = safe_creator_folder_name(metadata.get("channel"))
        if uploader_id == folder_id and username:
            candidates.add(username)
    return candidates


def find_creator_folder_migrations(
    output_dir: Path,
    explicit_mappings: Iterable[str] = (),
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Find verified numeric-to-username folder renames without changing files."""
    root = output_dir / "Instagram"
    if root.is_symlink():
        raise ValueError(f"Refusing to migrate through symlink: {root}")
    if not root.is_dir():
        return [], [f"No Instagram folder found: {root}"]

    explicit: dict[str, str] = {}
    for mapping in explicit_mappings:
        if "=" not in mapping:
            raise ValueError(f"Invalid --map value {mapping!r}; expected OLD_ID=USERNAME")
        old_id, username = mapping.split("=", 1)
        username = safe_creator_folder_name(username)
        if not old_id.isdigit() or not username:
            raise ValueError(f"Invalid --map value {mapping!r}; expected numeric ID and username")
        if old_id in explicit and explicit[old_id] != username:
            raise ValueError(f"Multiple usernames supplied for folder {old_id}")
        explicit[old_id] = username

    existing_dirs = [
        path for path in root.iterdir()
        if path.is_dir() and not path.is_symlink()
    ]
    existing_casefold = {path.name.casefold() for path in root.iterdir()}
    plans: list[tuple[Path, Path]] = []
    messages: list[str] = []
    planned_casefold: set[str] = set()

    for folder in sorted(existing_dirs, key=lambda path: path.name):
        if not folder.name.isdigit():
            continue
        media_ids = set()
        for path in folder.iterdir():
            if not path.is_file() or path.is_symlink():
                continue
            media_ids.add(
                path.name.removesuffix(".info.json")
                if path.name.endswith(".info.json")
                else path.stem
            )
        candidates = _metadata_creator_candidates(folder, folder.name)
        candidates.update(_manifest_creator_candidates(output_dir / "manifests", media_ids))
        if folder.name in explicit:
            candidates = {explicit[folder.name]}

        if len(candidates) != 1:
            reason = "no verified username" if not candidates else "ambiguous usernames: " + ", ".join(sorted(candidates))
            messages.append(f"SKIP {folder.name}: {reason}")
            continue

        username = next(iter(candidates))
        target = root / username
        target_casefold = username.casefold()
        if target_casefold in existing_casefold and target_casefold != folder.name.casefold():
            messages.append(f"SKIP {folder.name}: destination already exists ({target.name})")
            continue
        if target_casefold in planned_casefold:
            messages.append(f"SKIP {folder.name}: destination conflicts with another planned rename ({target.name})")
            continue
        if target_casefold == folder.name.casefold():
            messages.append(f"SKIP {folder.name}: username is unchanged")
            continue
        planned_casefold.add(target_casefold)
        plans.append((folder, target))

    return plans, messages


def migrate_creator_folders(output_dir: Path, apply: bool, explicit_mappings: Iterable[str]) -> None:
    plans, messages = find_creator_folder_migrations(output_dir, explicit_mappings)
    for message in messages:
        print(message)
    for source, target in plans:
        action = "RENAME" if apply else "WOULD RENAME"
        print(f"{action} {source.name} -> {target.name}")

    if not plans:
        print("No verified creator folders can be migrated.")
        return
    if not apply:
        print("Dry run only. Re-run with --apply to rename these folders.")
        return

    for source, target in plans:
        source.rename(target)
    print(f"Renamed {len(plans)} creator folder(s).")


def extract_post_urls_from_hrefs(hrefs: Iterable[str]) -> list[str]:
    seen_shortcodes: set[str] = set()
    urls: list[str] = []

    for href in hrefs:
        if not href:
            continue
        parsed = urlparse(href)
        if parsed.scheme or parsed.netloc:
            if parsed.netloc.lower().removeprefix("www.") != "instagram.com":
                continue
            path = parsed.path
        else:
            path = urlparse("https://www.instagram.com" + href).path
        match = POST_PATH_RE.match(path)
        if not match:
            continue
        kind, shortcode = match.group(1).lower(), match.group(2)
        if kind == "reels":
            kind = "reel"
        if shortcode in seen_shortcodes:
            continue
        canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
        seen_shortcodes.add(shortcode)
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
                link => /^\\/(?:[^/?#]+\\/)?(?:p|reels?(?!\\/audio\\/)|tv)\\/[^/?#]+\\/?$/i.test(
                    new URL(link.href).pathname
                )
            )
            """,
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


def collect_new_profile_media_hrefs(page) -> list[str]:
    """Return only media links that have not been seen on an earlier scroll."""
    return page.eval_on_selector_all("a[href]", COLLECT_NEW_MEDIA_HREFS_JS)


def wait_for_new_profile_media_link(page, timeout_ms: float) -> None:
    """Wait for lazy-loaded media, returning early as soon as a new link appears."""
    if timeout_ms <= 0:
        return
    try:
        page.wait_for_function(HAS_NEW_MEDIA_HREF_JS, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


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


def _collect_profile_surface(
    page,
    surface_url: str,
    surface_name: str,
    found: dict[str, None],
    found_shortcodes: set[str],
    max_scrolls: int,
    scroll_pause: float,
    max_posts: int | None,
) -> bool:
    """Collect one profile tab into the shared media URL set."""
    page.goto(surface_url, wait_until="domcontentloaded", timeout=60000)

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

    loaded_media = wait_for_profile_media_links(page)
    if not loaded_media:
        eprint(
            f"No post/reel links appeared in the {surface_name} tab during the initial "
            f"20-second wait (page URL: {page.url}; anchors: {page.locator('a[href]').count()})."
        )

    last_count = len(found)
    idle_rounds = 0
    for scroll_num in range(max_scrolls + 1):
        hrefs = collect_new_profile_media_hrefs(page)
        for url in extract_post_urls_from_hrefs(hrefs):
            shortcode = urlparse(url).path.rstrip("/").split("/")[-1]
            if shortcode in found_shortcodes:
                continue
            found_shortcodes.add(shortcode)
            found.setdefault(url, None)
            if max_posts and len(found) >= max_posts:
                break

        print(f"Collected {len(found)} post/reel URLs after {surface_name} scroll {scroll_num}/{max_scrolls}")

        if max_posts and len(found) >= max_posts:
            break

        if len(found) == last_count:
            idle_rounds += 1
        else:
            idle_rounds = 0
            last_count = len(found)

        if idle_rounds >= 4 and scroll_num >= 4:
            print(f"No new URLs found in the {surface_name} tab after several scrolls; stopping collection.")
            break

        if scroll_num >= max_scrolls:
            break

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        wait_for_new_profile_media_link(page, max(0.0, scroll_pause) * 1000)

    return loaded_media


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
    found_shortcodes: set[str] = set()
    surfaces = (
        (profile_url, "profile"),
        (f"{profile_url.rstrip('/')}/reels/", "Reels"),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel=channel)
        context_kwargs = {}
        if storage_state and storage_state.exists():
            context_kwargs["storage_state"] = str(storage_state)
        context = browser.new_context(**context_kwargs)
        for surface_url, surface_name in surfaces:
            if max_posts and len(found) >= max_posts:
                break
            page = context.new_page()
            try:
                _collect_profile_surface(
                    page=page,
                    surface_url=surface_url,
                    surface_name=surface_name,
                    found=found,
                    found_shortcodes=found_shortcodes,
                    max_scrolls=max_scrolls,
                    scroll_pause=scroll_pause,
                    max_posts=max_posts,
                )
            except Exception as exc:
                if surface_name != "Reels":
                    raise
                eprint(f"Could not collect the Reels tab ({exc}); keeping collected profile URLs.")
            finally:
                page.close()

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
        # Instagram's channel field is the username; uploader_id is numeric.
        "outtmpl": str(output_dir / "%(extractor)s" / "%(channel,uploader_id|unknown)s" / "%(id)s.%(ext)s"),
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
    if write_thumbnail:
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


def download_extracted_media(
    urls: list[str],
    output_dir: Path,
    archive_path: Path,
    cookie_file: Path | None,
    write_metadata: bool,
    write_thumbnail: bool,
    images_only: bool,
    max_access_failures: int = 3,
) -> None:
    logger = DownloadLogger()
    circuit_breaker = AccessCircuitBreaker(max_access_failures)
    opts = build_ydl_options(
        output_dir=output_dir,
        archive_path=archive_path,
        cookie_file=cookie_file,
        write_metadata=write_metadata,
        write_thumbnail=write_thumbnail,
        # Raw extraction keeps image entries available for manual processing.
        images_only=True,
    )
    opts["logger"] = logger
    image_count = 0
    video_count = 0

    with YoutubeDL(opts) as ydl:
        for url in urls:
            error_count = len(logger.errors)
            info = None
            try:
                info = ydl.extract_info(url, download=False, process=False)
            except DownloadError as exc:
                eprint(f"Could not inspect {url}: {exc}")
                logger.errors.append(str(exc))
            else:
                if info:
                    for entry in iter_media_entries(info):
                        for key in (
                            "extractor",
                            "extractor_key",
                            "webpage_url",
                            "original_url",
                            "channel",
                            "uploader",
                            "uploader_id",
                        ):
                            if info.get(key) and not entry.get(key):
                                entry[key] = info[key]

                        if entry.get("formats"):
                            if not images_only:
                                if ydl.process_ie_result(entry, download=True):
                                    video_count += 1
                            continue
                        image_info = build_image_download_info(entry)
                        if not image_info:
                            continue
                        ydl.process_info(image_info)
                        if image_info.get("__write_download_archive") is True:
                            ydl.record_download_archive(image_info)
                            image_count += 1

            circuit_breaker.record(url, logger.errors[error_count:], succeeded=info is not None)

    if images_only:
        print(f"Processed {image_count} image(s).")
    else:
        print(f"Processed {video_count} video(s) and {image_count} image(s).")


def download_urls(
    urls: list[str],
    output_dir: Path,
    archive_path: Path,
    cookie_file: Path | None,
    write_metadata: bool,
    write_thumbnail: bool,
    images_only: bool,
    max_access_failures: int = 3,
) -> None:
    if not urls:
        print("No URLs to download.")
        return

    if not images_only and not shutil.which("ffmpeg"):
        eprint("Warning: ffmpeg was not found on PATH. Videos may not merge/remux correctly.")

    download_extracted_media(
        urls=urls,
        output_dir=output_dir,
        archive_path=archive_path,
        cookie_file=cookie_file,
        write_metadata=write_metadata,
        write_thumbnail=write_thumbnail,
        images_only=images_only,
        max_access_failures=max_access_failures,
    )


def make_plan(args: argparse.Namespace) -> DownloadPlan:
    profile_url = normalize_instagram_url(args.profile_url)
    output_dir = Path(args.output).expanduser().resolve()
    manifest_dir = output_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    username = [p for p in urlparse(profile_url).path.split("/") if p][0]
    manifest_path = manifest_dir / f"{username}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    return DownloadPlan(profile_url=profile_url, urls=[], manifest_path=manifest_path, output_dir=output_dir)


def command_download(args: argparse.Namespace) -> None:
    if args.max_access_failures < 1:
        raise ValueError("--max-access-failures must be at least 1")

    plan = make_plan(args)
    storage_state = Path(args.storage_state).expanduser().resolve() if args.storage_state else None
    cookie_file = Path(args.cookies).expanduser().resolve() if args.cookies else None
    archive_path = plan.output_dir / "downloaded_archive.txt"

    with workspace_run_lock(Path(".ig_media_downloader.lock").resolve()):
        if args.manifest:
            source_manifest = Path(args.manifest).expanduser().resolve()
            urls = load_manifest_urls(source_manifest, plan.profile_url)
            print(f"Reusing {len(urls)} post/reel URLs from manifest: {source_manifest}")
        elif args.direct_only:
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

        if args.also_try_profile_url and not args.direct_only:
            urls.append(plan.profile_url)
            # De-duplicate after optional profile URL append.
            urls = list(dict.fromkeys(urls))

        if not args.manifest:
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
            max_access_failures=args.max_access_failures,
        )
        print(f"Done. Downloads are under: {plan.output_dir}")


def command_migrate_folders(args: argparse.Namespace) -> None:
    output_dir = Path(args.output).expanduser().resolve()
    with workspace_run_lock(Path(".ig_media_downloader.lock").resolve()):
        migrate_creator_folders(
            output_dir=output_dir,
            apply=args.apply,
            explicit_mappings=args.map,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ig-scraper",
        description="Download media from Instagram profiles that are publicly accessible or accessible to your own logged-in session.",
        epilog="Shortcut: ig-scraper --user USERNAME [download options]",
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
        "--max-access-failures",
        type=int,
        default=3,
        help="Stop after this many consecutive access, missing-content, or rate-limit errors. Default: 3",
    )
    dl.add_argument(
        "--images-only",
        action="store_true",
        help="Download still-image posts and image carousel slides, skipping videos and Reels.",
    )
    dl.add_argument("--also-try-profile-url", action="store_true", help="Also pass the profile URL directly to yt-dlp after collected URLs.")
    source = dl.add_mutually_exclusive_group()
    source.add_argument("--direct-only", action="store_true", help="Skip Playwright collection and pass the profile URL directly to yt-dlp.")
    source.add_argument(
        "--manifest",
        help="Reuse URLs from an earlier manifest instead of collecting the profile again.",
    )
    dl.set_defaults(func=command_download)

    migrate = subparsers.add_parser(
        "migrate-folders",
        help="Safely rename verified numeric creator folders to usernames.",
    )
    migrate.add_argument("--output", default="downloads", help="Output folder containing Instagram/. Default: downloads")
    migrate.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD_ID=USERNAME",
        help="Explicitly map a numeric folder when manifests cannot verify it; repeat as needed.",
    )
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Apply the displayed renames. Without this flag, only show a dry-run preview.",
    )
    migrate.set_defaults(func=command_migrate_folders)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    try:
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        args = parser.parse_args(expand_user_shortcut(raw_argv))
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit("Interrupted.")
    except Exception as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
