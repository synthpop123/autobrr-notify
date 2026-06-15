# autobrr-notify

A tiny [FastAPI](https://fastapi.tiangolo.com/) service that turns an [autobrr](https://autobrr.com/)
**Webhook** filter action into a rich Telegram notification. autobrr POSTs a matched
release, the service enriches it with [TMDB](https://www.themoviedb.org/) metadata
(by IMDB id when available, otherwise a title + year search) and posts a MarkdownV2
card — poster, director, cast, genres, size/runtime/bitrate, overview and inline links
to TMDB / IMDB / Letterboxd / Douban — to your channel.

## Demo

<img src="https://img.lkwplus.com/6lZeGs7XIQT2w80H.webp" alt="example notification" style="width: 45%;">

## Features

- Enriches releases with TMDB metadata: title, year, director, cast, genres, overview, poster.
- IMDB id lookup with automatic fallback to a title + year search.
- Rich MarkdownV2 card with a poster preview and inline DB links (TMDB / IMDB / Letterboxd / Douban).
- Bilingual labels: Chinese for `zh*` languages, English otherwise; content follows `TMDB_LANGUAGE`.
- Non-blocking: replies to autobrr instantly and runs the lookup/send in the background.
- Resilient: retries transient TMDB/Telegram errors and falls back to a plain card when TMDB has no match.
- Ships as a small, non-root container — reachable over the network, no host port required.

## How it works

```text
autobrr filter ──(Webhook action, JSON)──▶ autobrr-notify ──▶ TMDB ──▶ Telegram channel
```

## Quick start

```bash
git clone https://github.com/synthpop123/autobrr-notify.git
cd autobrr-notify
cp .env.example .env       # fill in your TMDB + Telegram values
docker compose up -d --build
```

The bundled `docker-compose.yml` runs the notifier (and, as an example, autobrr
itself). If you already run autobrr, delete that service and attach `autobrr-notify`
to autobrr's network instead — autobrr only needs to reach it in-network at
`http://autobrr-notify:8000/notify`.

Run it standalone instead:

```bash
docker build -t autobrr-notify .
docker run -d --name autobrr-notify -p 8000:8000 --env-file .env autobrr-notify
```

## Configuration

Everything is read from environment variables (see `.env.example`):

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TMDB_API_KEY` | yes | — | TMDB API key ([settings/api](https://www.themoviedb.org/settings/api)). |
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHANNEL_ID` | yes | — | Target channel id (e.g. `-1001234567890`); the bot must be a channel admin. |
| `TMDB_LANGUAGE` | no | `zh-CN` | TMDB language code. `zh*` ⇒ Chinese labels, otherwise English. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

## autobrr setup

In your filter, add an **Action** of type **Webhook**:

- **Method**: `POST`
- **Host / URL**: `http://autobrr-notify:8000/notify` (whatever address autobrr can reach the service at)
- **Data (JSON)**: one of the payloads below.

The service matches on the IMDB id first and falls back to a title + year search, so
prefer the payload **with** `meta_imdb` for filters that expose it:

```json
{
  "torrent_name": "{{ .TorrentName }}",
  "parsed_title": "{{ .Title }}",
  "indexer_name": "{{ .IndexerName }}",
  "group_name": "{{ .Group }}",
  "release_year": "{{ .Year }}",
  "file_size": {{ .Size }},
  "meta_imdb": "{{ .MetaIMDB }}"
}
```

Without an IMDB id (title + year search only) — just omit `meta_imdb`:

```json
{
  "torrent_name": "{{ .TorrentName }}",
  "parsed_title": "{{ .Title }}",
  "indexer_name": "{{ .IndexerName }}",
  "group_name": "{{ .Group }}",
  "release_year": "{{ .Year }}",
  "file_size": {{ .Size }}
}
```

`file_size` is intentionally unquoted: `{{ .Size }}` is the size in bytes and feeds the
size/bitrate figures. Only `torrent_name` and `parsed_title` are strictly required.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/notify` | autobrr webhook target. Returns `200` immediately; enrichment + send run in the background. |
| `GET` | `/healthz` | Liveness probe; reports whether the required config is present. |

If the port is exposed (standalone or dev), you can smoke-test without autobrr:

```bash
curl -fsS localhost:8000/healthz
curl -fsS -X POST localhost:8000/notify -H 'content-type: application/json' \
  -d '{"torrent_name":"Some.Movie.2014.1080p.BluRay-GRP","parsed_title":"Some Movie","release_year":"2014","file_size":12300000000,"meta_imdb":"tt0816692"}'
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in values
uvicorn app:app --reload --port 8000
```

## License

[MIT](./LICENSE)
