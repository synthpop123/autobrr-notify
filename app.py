"""autobrr -> Telegram movie-release notifier.

A small FastAPI service that turns an autobrr "Webhook" filter action into a rich
Telegram notification. autobrr POSTs a matched release here; the service looks the
movie up on TMDB (by IMDB id when available, otherwise by title + year) and posts a
MarkdownV2 card — poster, director/cast, genres, size/runtime/bitrate, overview and
inline links to TMDB / IMDB / Letterboxd / Douban — to a channel.

Endpoints:
  POST /notify   autobrr webhook target. Replies 200 immediately and performs the TMDB
                 lookup + Telegram send in the background, so autobrr never blocks.
  GET  /healthz  liveness + config sanity.

All configuration is read from environment variables (see .env.example). The field
labels follow the configured language (zh-CN or English); the movie content language
follows TMDB_LANGUAGE.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import requests
from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("autobrr-notify")

TELEGRAM_API = "https://api.telegram.org"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"

# Field labels per language. The card content (title, overview, ...) follows
# TMDB_LANGUAGE; these labels just localize the field names and units.
LABELS = {
    "zh-CN": {
        "name": "名称", "director": "导演", "cast": "演员", "genres": "类型",
        "title": "标题", "info": "信息", "minutes": "分钟", "unknown": "未知", "sep": "：",
    },
    "en": {
        "name": "Name", "director": "Director", "cast": "Cast", "genres": "Genres",
        "title": "Title", "info": "Info", "minutes": "min", "unknown": "Unknown", "sep": ": ",
    },
}


def labels_for(language: str) -> dict[str, str]:
    return LABELS["zh-CN"] if language.lower().startswith("zh") else LABELS["en"]


@dataclass
class Config:
    """Runtime configuration, read from the environment."""

    tmdb_api_key: str = field(default_factory=lambda: os.getenv("TMDB_API_KEY", ""))
    tmdb_language: str = field(default_factory=lambda: os.getenv("TMDB_LANGUAGE", "zh-CN"))
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_channel_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID", ""))

    def missing(self) -> list[str]:
        """Required keys that are still unset (used by /healthz and startup)."""
        required = {
            "TMDB_API_KEY": self.tmdb_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHANNEL_ID": self.telegram_channel_id,
        }
        return [name for name, value in required.items() if not value]


class Release(BaseModel):
    """Payload from an autobrr Webhook action (one matched release).

    Field names mirror the macros wired in the action's Data box, e.g.:
      {"torrent_name":"{{ .TorrentName | js }}","indexer_name":"{{ .FilterName | js }}",
       "group_name":"{{ .Group | js }}","release_year":"{{ .Year }}",
       "parsed_title":"{{ .Title | js }}","file_size":{{ .Size }},
       "meta_imdb":"{{ .MetaIMDB }}"}
    Only torrent_name + parsed_title are required; meta_imdb is optional (IMDB id wins
    over the title/year search when present).
    """

    torrent_name: str
    parsed_title: str
    indexer_name: str = ""
    group_name: str = ""
    release_year: str = ""
    file_size: int = 0
    meta_imdb: Optional[str] = None


def _retrying_session() -> requests.Session:
    """A requests session that retries transient TMDB/Telegram 5xx errors."""
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


class MovieClient:
    """TMDB lookups: resolve a release to a TMDB id, then fetch localized details.

    Talks to the TMDB REST API through ONE pooled session. (tmdbv3api was dropped: it
    ignores any injected session and lru-caches whole `requests.request` responses,
    leaking a socket per call until the process hits its open-files limit.)
    """

    def __init__(self, config: Config):
        self.config = config
        self.session = _retrying_session()

    def _get(self, path: str, **params: Any) -> dict:
        params["api_key"] = self.config.tmdb_api_key
        resp = self.session.get(f"{TMDB_API}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @lru_cache(maxsize=256)
    def search_movie(self, title: str, year: Optional[str]) -> list[dict]:
        params: dict[str, Any] = {"query": title, "language": self.config.tmdb_language}
        if year:
            params["year"] = year
        return self._get("/search/movie", **params).get("results") or []

    @lru_cache(maxsize=256)
    def find_by_imdb(self, imdb_id: str) -> Optional[dict]:
        results = self._get(f"/find/{imdb_id}", external_source="imdb_id").get("movie_results") or []
        return results[0] if results else None

    @lru_cache(maxsize=256)
    def details(self, tmdb_id: int, language: Optional[str] = None) -> dict:
        """Movie details with credits + images, optionally in another language."""
        return self._get(
            f"/movie/{tmdb_id}",
            append_to_response="credits,images",
            language=language or self.config.tmdb_language,
        )

    def resolve_id(self, release: Release) -> Optional[int]:
        """Find a TMDB id from the IMDB id (if present) or a title+year search."""
        if release.meta_imdb:
            hit = self.find_by_imdb(release.meta_imdb)
            if hit:
                return hit["id"]
            log.warning("IMDB id %s not found; falling back to title/year", release.meta_imdb)
        results = self.search_movie(release.parsed_title, release.release_year or None)
        return results[0]["id"] if results else None


class MessageFormatter:
    """Render the Telegram MarkdownV2 message body (labels follow the language)."""

    # Characters that MarkdownV2 requires to be backslash-escaped.
    SPECIAL = "_*[]()~`>#+-=|{}.!"

    def __init__(self, language: str):
        self.labels = labels_for(language)

    @classmethod
    def escape(cls, text: str) -> str:
        text = str(text)
        for char in cls.SPECIAL:
            text = text.replace(char, f"\\{char}")
        return text

    @staticmethod
    def names(people: list[dict], max_count: int = 3, job: Optional[str] = None) -> str:
        """Up to `max_count` person names; if `job` is given, only matching crew."""
        out: list[str] = []
        for person in people:
            if len(out) >= max_count:
                break
            if job and person.get("job") != job:
                continue
            out.append(person["name"])
        return ", ".join(out)

    def file_info(self, file_size: int, runtime: Optional[int]) -> tuple[str, str, str]:
        """Human-readable size / duration / bitrate (bitrate needs a runtime)."""
        unknown = self.labels["unknown"]
        size = f"{file_size / 1024 ** 3:.2f} GiB" if file_size else unknown
        if runtime:
            duration = f"{runtime} {self.labels['minutes']}"
            bitrate = (
                f"{file_size * 8 / (runtime * 60) / 1_000_000:.2f} Mbps" if file_size else unknown
            )
        else:
            duration = bitrate = unknown
        return size, duration, bitrate

    def rich(self, data: dict[str, Any]) -> str:
        """Full card: TMDB metadata, poster preview, overview."""
        esc, lab = self.escape, self.labels
        info = f"{esc(data['size'])} / {esc(data['duration'])} / {esc(data['bitrate'])}"
        if data["poster_url"]:
            # A zero-width char carrying a hidden link makes Telegram show the poster.
            info += f"[\u200b]({data['poster_url']})"
        rows = [
            (lab["name"], f"{esc(data['title'])} \\({esc(data['year'])}\\)"),
            (lab["director"], esc(data["director"])),
            (lab["cast"], esc(data["starring"])),
            (lab["genres"], esc(", ".join(data["genres"]))),
            (lab["title"], f"__{esc(data['torrent_name'])}__"),
            (lab["info"], info),
        ]
        body = f"\\#{esc(data['indexer'])} \\#{esc(data['group'])}\n"
        body += "\n".join(f"*{label}*{lab['sep']}{content}" for label, content in rows)
        if data["overview"]:
            body += f"\n\n> {esc(data['overview'])}"
        return body

    def plain(self, release: Release) -> str:
        """Fallback when TMDB has no match — still notify, just without metadata."""
        esc, lab = self.escape, self.labels
        size = f"{release.file_size / 1024 ** 3:.2f} GiB" if release.file_size else lab["unknown"]
        return (
            f"\\#{esc(release.indexer_name)} \\#{esc(release.group_name)}\n"
            f"*{lab['title']}*{lab['sep']}__{esc(release.torrent_name)}__\n"
            f"*{lab['info']}*{lab['sep']}{esc(size)}"
        )


class TelegramNotifier:
    """Post messages to a Telegram channel via the Bot API."""

    def __init__(self, config: Config):
        self.config = config
        self.session = _retrying_session()

    def send(self, text: str, links: Optional[dict[str, str]] = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.config.telegram_channel_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "link_preview_options": {
                "is_disabled": False,
                "prefer_large_media": True,
                "show_above_text": True,
            },
        }
        if links:
            buttons = [{"text": name, "url": url} for name, url in links.items() if url]
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": [buttons]}
        resp = self.session.post(
            f"{TELEGRAM_API}/bot{self.config.telegram_bot_token}/sendMessage",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()


CONFIG = Config()
MOVIES = MovieClient(CONFIG)
FORMATTER = MessageFormatter(CONFIG.tmdb_language)
NOTIFIER = TelegramNotifier(CONFIG)


def process_release(release: Release) -> None:
    """Enrich a release with TMDB data and notify; degrade to a plain card on any failure."""
    missing = CONFIG.missing()
    if missing:
        log.error("cannot notify, missing config: %s", ", ".join(missing))
        return
    try:
        tmdb_id = MOVIES.resolve_id(release)
        if tmdb_id is None:
            log.warning("no TMDB match for %r (%s); sending plain card",
                        release.parsed_title, release.release_year)
            NOTIFIER.send(FORMATTER.plain(release))
            return

        localized = MOVIES.details(tmdb_id)
        english = MOVIES.details(tmdb_id, language="en-US")
        credits = localized.get("credits") or {}
        size, duration, bitrate = FORMATTER.file_info(release.file_size, localized.get("runtime"))
        backdrop = english.get("backdrop_path") or english.get("poster_path")
        imdb_id = localized.get("imdb_id")
        data = {
            "title": localized.get("title") or english.get("title") or release.parsed_title,
            "year": (localized.get("release_date") or "").split("-")[0] or release.release_year,
            "overview": localized.get("overview") or english.get("overview") or "",
            "genres": [genre["name"] for genre in localized.get("genres") or []],
            "director": FORMATTER.names(credits.get("crew") or [], job="Director"),
            "starring": FORMATTER.names(credits.get("cast") or []),
            "size": size,
            "duration": duration,
            "bitrate": bitrate,
            "indexer": release.indexer_name,
            "group": release.group_name,
            "torrent_name": release.torrent_name,
            "poster_url": f"{TMDB_IMAGE_BASE}{backdrop}" if backdrop else "",
        }
        links = {
            "TMDB": f"https://www.themoviedb.org/movie/{tmdb_id}",
            "IMDB": f"https://www.imdb.com/title/{imdb_id}" if imdb_id else "",
            "Letterboxd": f"https://letterboxd.com/imdb/{imdb_id}/" if imdb_id else "",
            "Douban": f"https://search.douban.com/movie/subject_search?search_text={imdb_id}" if imdb_id else "",
        }
        NOTIFIER.send(FORMATTER.rich(data), links)
        log.info("notified: %s (%s)", data["title"], data["year"])
    except Exception:
        log.exception("notify failed for %r; attempting plain fallback", release.torrent_name)
        try:
            NOTIFIER.send(FORMATTER.plain(release))
        except Exception:
            log.exception("plain fallback also failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    missing = CONFIG.missing()
    if missing:
        log.warning("starting with INCOMPLETE config (missing: %s)", ", ".join(missing))
    else:
        log.info("autobrr-notify ready (tmdb language=%s)", CONFIG.tmdb_language)
    yield


app = FastAPI(title="autobrr-notify", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "config_complete": not CONFIG.missing()}


@app.post("/notify")
def notify(release: Release, background: BackgroundTasks) -> dict[str, Any]:
    log.info("queued: %s", release.torrent_name)
    background.add_task(process_release, release)
    return {"accepted": True}
