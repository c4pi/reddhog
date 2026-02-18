# 🦔 ReddHog — The Reddit Scraper

**Python Reddit scraper** — async, rate-limit resilient, with browser fallback. Scrape subreddits, posts, and comments to JSON, CSV, or Excel.

<p align="center">
  <img src="assets/logo.png" alt="ReddHog - Reddit scraper logo">
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
  <a href="#what-you-get"><img src="https://img.shields.io/badge/async-httpx-orange" alt="Async"></a>
  <a href="#what-you-get"><img src="https://img.shields.io/badge/browser-Patchright-8B5CF6?logo=googlechrome&logoColor=white" alt="Browser Fallback"></a>
  <a href="https://www.reddit.com"><img src="https://img.shields.io/badge/reddit-scraper-FF4500?logo=reddit&logoColor=white" alt="Reddit"></a>
</p>

---

## What you get

<a id="what-you-get"></a>
**A resilient Reddit scraper that keeps going when others give up.**

- **Rate-limit resilient** — circuit breaker pattern with automatic cooldowns; handles 429s, 403s, and 5xx gracefully.
- **Browser fallback** — switches to Patchright when JSON API is blocked, then resumes API when safe.
- **Async & fast** — concurrent requests with configurable parallelism via httpx.
- **Full comments** — nested comment trees with parent relationships and depth tracking.
- **Image scraping** — downloads post images and Reddit galleries.
- **Multiple exports** — JSON (primary), CSV, and Excel.
- **Incremental updates** — refresh existing posts without re-scraping everything.

Good for datasets, research, backups, or building on Reddit data without the official API.

## 📦 Install

ReddHog is **CLI-only** and **not on PyPI yet**. Install from source:

```bash
git clone https://github.com/c4pi/reddhog.git
cd reddhog
uv sync
```

**Alternatively, with pip instead of uv:**

```bash
pip install -r requirements.txt
pip install -e .
```

### Then install the Chrome driver (required)

ReddHog uses **Patchright** for browser-based fallback scraping. You **must** install the Chrome driver after dependencies:

```bash
patchright install chrome
```

### Run warmup first (recommended)

Before scraping, run **warmup** once to create browser profiles with real cookies and user agents. This reduces the chance of being blocked by Reddit:

```bash
reddhog warmup
```

- Opens a Chrome window, loads Reddit; you accept cookies, solve any CAPTCHAs, scroll a bit, then press ENTER.
- Saves storage state and User-Agent into a profile directory that the scraper reuses.
- For multiple browser profiles (e.g. rotation): `reddhog warmup --num-profiles 3`

**Without warmup** the tool still runs, but the JSON and browser clients fall back to a generic User-Agent that is easier for Reddit to detect and block. Running warmup is the recommended first step after installation.

### Now you can run reddhog

Run with `uv run reddhog ...` from the repo (or activate the venv and run `reddhog` directly).

## 🚀 Quick start

```bash
# Collect + scrape newest 50 new posts
reddhog subreddit wallpaper 50

# Scrape a single post with comments
reddhog url "https://reddit.com/r/python/comments/abc123/my_post/"

# Refresh existing data (e.g. update upvotes and comments for ./data/wallpaper/)
reddhog refresh wallpaper
```

Results are stored under `./data/<name>/` (e.g. `./data/python/`) as `data.json`, and optionally as Excel or CSV:

```
data/
└── <name>/           # e.g. python, wallpaper
    ├── data.json     # Full structured data (always written)
    ├── data.csv      # If --export csv or file already existed
    ├── data.xlsx     # If --export excel or file already existed
    ├── images/       # Downloaded media
    └── debug/        # Failure artifacts when browser extraction fails
```

## 📖 Usage

| Command                          | What it does                                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `reddhog warmup`                 | Warm browser profiles (run once after install). Creates profiles with real cookies/UAs for safer scraping.   |
| `reddhog version`                | Show version number                                                                                          |
| `reddhog settings`               | Show effective settings (app_env, debug, log_level)                                                          |
| `reddhog subreddit NAME [LIMIT]` | Scrape posts from a subreddit; results go to ./data/<name>/                                                  |
| `reddhog url URL`                | Scrape one post from a Reddit URL                                                                            |
| `reddhog refresh NAME [LIMIT]`   | Update existing data in ./data/<name>/ (upvotes, comments). Optional LIMIT = only refresh the N newest posts |

### Global options

| Option                                 | Default  | Description                                                                                                                                       |
| -------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--export [excel\|csv]`                | —        | Also write Excel or CSV next to data.json. If omitted, only data.json is created; existing .xlsx/.csv in the output folder are updated if present |
| `--headless` / `--no-headless`         | headless | Run the browser with no window. Use `--no-headless` to see the browser window (useful for debugging).                                             |
| `-c, --concurrency`                    | 6        | Number of requests to run in parallel. Lower if you hit rate limits                                                                               |
| `-s, --strategy [auto\|json\|browser]` | auto     | `auto`: JSON first with browser fallback. `json`: JSON-only. `browser`: browser-only for post scraping                                            |

### Strategy behavior

- `auto`: Listing and post scraping start with JSON; browser fallback is used when needed.
- `json`: JSON-only behavior; browser fallback is disabled while JSON cools down.
- `browser`: Listing still starts with JSON and can fallback to browser; post scraping is browser-only.

### Collecting vs scraping

`subreddit` runs in two steps:

1. Collect post stubs from the subreddit listing.
2. Scrape full details only for posts that are new to `data.json`.

Because of this, collected totals can be higher than the "Scraping new posts" count.

### Examples

```bash
# High concurrency for faster scraping
reddhog subreddit dataisbeautiful 100 -c 10

# Browser-focused scraping (slower)
reddhog subreddit wallpaper --strategy browser

# Show the browser window (debugging)
reddhog subreddit dataisbeautiful 3 --strategy browser --no-headless

# Also export to Excel
reddhog subreddit python --export excel

# Refresh only the 20 newest posts in an existing dataset
reddhog refresh python 20
```

## Configuration

### Logging

Control logging via the `.env` file:

```dotenv
LOG_LEVEL=DEBUG    # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

Check current log level with:

```bash
reddhog settings
```

### Timing & Environment

Timing constants (adjust in source if needed):

| Constant                            | Default  | Description                                         |
| ----------------------------------- | -------- | --------------------------------------------------- |
| `REQUEST_DELAY`                     | 1.5s     | Delay between requests                              |
| `TIMEOUT`                           | 30s      | Request timeout                                     |
| `IMG_CONCURRENCY`                   | 10       | Parallel image downloads                            |
| `BROWSER_COOLDOWN_FALLBACK_SECONDS` | 60s      | Browser cooldown when no explicit wait is available |
| `COOLDOWN_FALLBACK_MIN/MAX`         | 300–600s | Cooldown when no reset header                       |

Contributions are welcome — open an [issue](https://github.com/c4pi/reddhog/issues) or submit a PR.

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <strong>If ReddHog is useful to you, <a href="https://github.com/c4pi/reddhog">give it a ⭐ on GitHub</a>.</strong><br>
  <sub>Built with 🦔 grit</sub>
</p>
