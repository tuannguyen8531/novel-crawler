# Novel Crawler

CLI tool for downloading web novel chapters from public websites using CSS selector configuration.

## Features

- **Configurable selectors**: Per-site JSON config with CSS selectors for title, chapters, content
- **AI config generation**: Use Ollama or Gemini to auto-generate site configs from a TOC URL
- **EPUB import**: Split EPUB spine sections into `chapter_N.txt` translator input files
- **Auto-resume**: Skips already-downloaded chapters, continues from where it left off
- **Browser mode**: Playwright headless browser for sites with JavaScript challenges (Cloudflare)
- **Concurrent browser pages**: One Chromium session with isolated pages for parallel chapter downloads
- **robots.txt respect**: Checks site crawl rules by default
- **Atomic writes**: Crash-safe file output via temp file + rename
- **Incremental manifest**: Real-time progress tracking in `manifest.json`

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management
- [Playwright](https://playwright.dev) (for `--browser` mode), with Playwright Chromium or a system Chrome/Chromium browser

## Setup

```bash
# Install dependencies
uv sync

# Install Playwright browser (for --browser mode)
uv run playwright install chromium

# Configure environment (optional)
cp .env.example .env
```

### Environment Variables

```env
# Shared output directory for chapter text files
NOVEL_SHARE_DIR=../share

# Default max chapters per crawl (0 = unlimited)
MAX_CHAPTERS=0

# Use headless browser by default (true/false)
USE_BROWSER=false

# LLM provider for AI config generation (ollama or gemini)
LLM_PROVIDER=gemini
LLM_TEMPERATURE=0.0
LLM_MAX_TOKENS=4096

# Ollama settings (for LLM_PROVIDER=ollama)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3

# Gemini settings (for LLM_PROVIDER=gemini)
GEMINI_API_KEY=your-api-key
GEMINI_MODEL=gemini-2.5-flash
```

With this layout, three directories sit side by side:

```text
Personal/
  share/
  novel-crawler/
  novel-translator/
```

## Usage

### 1. Create a site config

**Option A: AI-generated (recommended)**

```bash
# Use default LLM provider (from .env)
uv run generate "https://example.com/novel/table-of-contents"

# Specify provider and config name
uv run generate "https://example.com/novel/toc" --provider gemini --name my-novel

# Use browser for JS-heavy sites
uv run generate "https://example.com/novel/toc" --browser
```

The AI analyzes the TOC page and a sample chapter page, then shows the generated config for review before saving.

**Option B: Manual**

```bash
mkdir -p configs
cp examples/site-config.example.json configs/my-site.json
```

Edit `configs/my-site.json` with CSS selectors matching the target website:

```json
{
  "name": "example-public-site",
  "start_url": "https://example.com/novel/table-of-contents",
  "novel_title_selector": "h1",
  "author_selector": ".author",
  "chapter_link_selector": ".chapter-list a",
  "toc_next_selector": "a.next",
  "chapter_title_selector": "h1",
  "chapter_content_selector": ".chapter-content",
  "remove_selectors": ["script", "style", ".ads"],
  "same_domain": true,
  "reverse_chapter_order": false,
  "request_delay_seconds": 1.5,
  "timeout_seconds": 60,
  "retry_attempts": 3,
  "retry_backoff_seconds": 2
}
```

### 2. Test the config

```bash
# Quick selector validation (1 TOC + 1 chapter fetch)
uv run validate my-site

# With browser for JS-heavy sites
uv run validate my-site --browser

# Full dry-run preview
uv run crawl my-site --dry-run --max 5
```

### 3. Download chapters

```bash
# Standard mode (urllib with browser headers)
uv run crawl my-site

# Browser mode (Playwright, for Cloudflare/JS challenges)
uv run crawl my-site --browser

# Opt in to 3 concurrent browser pages in one Chromium session
uv run crawl my-site --browser --workers 3

# Download next 20 new chapters (skips don't count)
uv run crawl my-site --browser --max 20

# Re-download all chapters
uv run crawl my-site --browser --overwrite
```

### 4. Import an EPUB

```bash
# Import EPUB into ../share/{slug}/input/
uv run import path/to/book.epub

# Override the output slug
uv run import path/to/book.epub -n military-training

# Use a different share root
uv run import path/to/book.epub -n military-training --share-output ../share
```

The importer reads EPUB OPF metadata for title and author, extracts readable spine sections,
normalizes chapter files to `chapter_N.txt`, copies referenced images into `illustrations/`,
and writes metadata beside `input/`.

### crawl Options

| Flag | Description |
|------|-------------|
| `target` | Config path or novel name (matches `configs/{novel}.json`) |
| `-b, --browser` | Use headless browser (Playwright) for sites with JS challenges. Default: `USE_BROWSER` env |
| `-m, --max N` | Stop after fetching N new chapters (skipped chapters don't count). Default: `MAX_CHAPTERS` env |
| `-w, --workers N` | Concurrent chapter downloads. Default: 1. Browser workers share one Chromium context |
| `--share-output PATH` | Override shared chapter output directory |
| `--overwrite` | Re-download chapters even if files already exist |
| `--fail-fast` | Stop on the first chapter error |
| `--ignore-robots` | Skip robots.txt check (use only with permission) |
| `--dry-run` | Discover chapters and print preview without downloading |

### generate Options

| Flag | Description |
|------|-------------|
| `url` | URL of the novel's table-of-contents page |
| `--name NAME` | Config name (default: derived from URL) |
| `--provider PROVIDER` | LLM provider override (`ollama` or `gemini`) |
| `-b, --browser` | Use headless browser to fetch pages |
| `--no-cache` | Skip the HTML cache and always re-fetch |
| `--output PATH` | Output directory (default: `configs`) |

### validate Options

| Flag | Description |
|------|-------------|
| `target` | Config path or novel name (matches `configs/{novel}.json`) |
| `-b, --browser` | Use headless browser to fetch pages |

### import Options

| Flag | Description |
|------|-------------|
| `epub` | EPUB file path to import |
| `-n, --name NAME` | Output slug name. Defaults to EPUB title or filename |
| `--share-output PATH` | Override shared output root. Default: `NOVEL_SHARE_DIR` or `../share` |
| `--keep-existing` | Keep existing `chapter_*.txt` files in the target input directory |

## How it works

### Crawl workflow

1. Reads site config from `configs/{novel}.json`
2. Fetches the table of contents page, extracts chapter links via CSS selector
3. Paginates through TOC if `toc_next_selector` is set (up to `max_toc_pages`)
4. For each chapter: fetches page, extracts content via selector, strips noise elements
5. Writes `chapter_N.txt` to shared input directory (atomic write)
6. Updates `manifest.json` after each chapter
7. On resume: skips chapters where `chapter_N.txt` exists and is non-empty
8. `--max` counts only newly fetched chapters, not skipped ones

### EPUB import workflow

1. Reads `META-INF/container.xml` and the OPF package document
2. Extracts `dc:title` and `dc:creator` when present
3. Reads each readable document in EPUB spine order
4. Uses explicit chapter markers such as `Chapter 1`, `Chương 1`, `第1章`, or `1화` when present
5. Falls back to spine order for title-only EPUBs, skipping cover/nav/notice/front matter
6. Writes `chapter_N.txt`, `illustrations/003-001.jpg`, and `metadata.json` to `../share/{novel}/`

### Output

Crawler and EPUB import outputs share the same translator input layout:

- `../share/{novel}/`: Translator input files and metadata
- `output/{novel}/`: Crawler runtime state for web crawls

In `../share/{novel}/`:
- `input/chapter_1.txt`, `input/chapter_2.txt`, ...: Individual chapter files
- `illustrations/003-001.jpg`, ...: EPUB images named by chapter number and image number within that chapter when importing EPUBs
- `metadata.json`: Novel title, translation placeholders, author, source URL, illustration URL, site name

In `output/{novel}/` for web crawls:
- `config.json`: Snapshot of the config used for this crawl
- `metadata.json`: Novel title, translation placeholders, author, source URL, illustration URL, site name
- `manifest.json`: Progress, discovered chapters, results, errors

### Console output

```
[1/1001] Chapter title
[2/1001] Chapter title
[3/1001] Chapter title (fail: error message)
Done: Novel Title (3 new, 60 skipped)
```

Skipped chapters are not printed. The summary line shows fetched vs skipped counts.

## Config Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Site identifier, used for output directory |
| `start_url` | Yes | Table of contents or chapter list page |
| `chapter_link_selector` | Yes | CSS selector for chapter links in TOC |
| `chapter_content_selector` | Yes | CSS selector for chapter body content |
| `novel_title_selector` | No | CSS selector for novel title on TOC page |
| `author_selector` | No | CSS selector for author name |
| `toc_next_selector` | No | CSS selector for "next page" button on TOC |
| `chapter_title_selector` | No | CSS selector for chapter title on chapter page |
| `remove_selectors` | No | CSS selectors for elements to strip (ads, nav, etc.) |
| `same_domain` | No | Only follow links on same domain (default: true) |
| `reverse_chapter_order` | No | Reverse chapter order if TOC shows newest first |
| `request_delay_seconds` | No | Delay between requests (default: 1.0) |
| `timeout_seconds` | No | Request timeout (default: 30.0) |
| `retry_attempts` | No | Retry count for network errors (default: 3) |
| `retry_backoff_seconds` | No | Base backoff for retries (default: 2.0) |
| `max_toc_pages` | No | Max TOC pages to paginate through (default: 50) |
| `user_agent` | No | Custom User-Agent header |

## Architecture

```
fetch → parse → extract → clean → write → track
```

| Component | Purpose |
|-----------|---------|
| `cli.py` | Argument parsing, entry points, progress display |
| `config.py` | App-level Config (from_env) + SiteConfig (per-site JSON), python-dotenv |
| `models/` | Data models: ChapterLink, NovelMetadata, ChapterResult, CrawlProgress |
| `services/crawler.py` | NovelCrawler: chapter discovery, crawl loop, manifest tracking |
| `services/config_generator.py` | AI-assisted site config generation (2-phase: TOC + chapter) |
| `services/epub_importer.py` | EPUB import: OPF metadata, spine text extraction, chapter normalization |
| `services/metadata.py` | Shared metadata serialization for crawler and EPUB import outputs |
| `services/http.py` | HttpClient: stdlib urllib, cookies, robots.txt, retry, encoding detection |
| `services/browser.py` | BrowserFetcher: async Playwright pool with one shared Chromium context; falls back to system Chrome when the bundled browser is unavailable |
| `services/llm/` | LLM providers: Ollama, Gemini with retry, logging, spinner |
| `utils/text.py` | Text utilities: slugify, normalize, HTML-to-text conversion |
| `utils/html.py` | HTML cleaning for LLM analysis: strip noise, keep structure |

## Project Structure

```
├── main.py                # Direct Python entry point
├── src/
│   ├── __init__.py
│   ├── cli.py             # CLI with argparse (crawl / generate / validate / import)
│   ├── config.py          # SiteConfig + python-dotenv + LLM settings
│   ├── models/
│   │   └── __init__.py    # ChapterLink, NovelMetadata, ChapterResult, CrawlResult, CrawlProgress
│   ├── services/
│   │   ├── __init__.py
│   │   ├── crawler.py     # NovelCrawler: discovery, crawl loop, manifest
│   │   ├── config_generator.py  # AI config generator (2-phase: TOC + chapter)
│   │   ├── epub_importer.py  # EPUB import into shared translator input
│   │   ├── metadata.py    # Shared metadata JSON serialization
│   │   ├── http.py        # HttpClient: urllib-based fetcher with retry/cookies
│   │   ├── browser.py     # BrowserFetcher: Playwright Chromium fetcher
│   │   └── llm/           # LLM provider abstraction
│   │       ├── __init__.py
│   │       ├── base.py    # BaseProvider: retry, logging, spinner
│   │       ├── factory.py # Provider factory (ollama, gemini)
│   │       ├── ollama.py  # OllamaProvider: local LLM via HTTP
│   │       └── gemini.py  # GeminiProvider: Google AI API
│   └── utils/
│       ├── __init__.py
│       ├── text.py        # slugify, normalize_text, html_to_plain_text
│       └── html.py        # HTML cleaning for LLM analysis
├── tests/                 # Test suite grouped by component
│   ├── test_cli.py        # CLI argument parsing, config resolution
│   ├── test_crawler.py    # Crawl workflow, resume, overwrite, progress
│   ├── test_epub_importer.py  # EPUB metadata extraction and chapter normalization
│   ├── test_env.py        # Config loading, defaults
│   └── test_http.py       # HTTP retry behavior
├── configs/               # Per-site JSON configurations
├── examples/
│   └── site-config.example.json  # Template config
├── output/                # Crawler runtime state (manifest, metadata, config snapshots)
└── .env.example           # Environment variable template
```

## Testing

```bash
uv run python -m unittest discover -s tests
```

Individual test files:

```bash
uv run python -m unittest tests/test_crawler.py
uv run python -m unittest tests/test_epub_importer.py
uv run python -m unittest tests/test_http.py
uv run python -m unittest tests/test_cli.py
```

## Notes

Only use with websites that permit public access and allow crawling per their terms of service. If `robots.txt` blocks a URL, the crawler stops. The `--ignore-robots` flag should only be used when you have explicit permission from the site owner.
