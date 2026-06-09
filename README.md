# ai_news_feed

A colorful, geometric curses TUI for AI news. Pulls ~30 RSS feeds in parallel,
enriches them with **GPT-5.4** via the OpenAI **Responses API**, and presents
everything as a stacked composite: a scrolling **marquee** of the day's most
important story, the ⚡ **banner** (counts + one-word mood + sentiment mix),
side-by-side 🌐 **THEME** and 🔎 **LIVE CONTEXT** panels, and a **two-pane
browser** — topic clusters on the left, that topic's stories on the right —
where Space/Enter opens a framed **story page**.

## Install

```bash
pipx install "ainews[all]"   # standalone command on your PATH (full experience)
ainews                       # launch
```

`[all]` pulls the optional `openai` + `trafilatura` reader stack; a bare
`pipx install ainews` still runs (raw feed, no GPT enrichment / reader).

### From source

```bash
git clone https://github.com/gmiv/ainews && cd ainews
pip install -e ".[all]"
ainews                       # or:  python -m ainews  /  python ai_news_feed.py
```

## Configure

Set your OpenAI key for the GPT-5.4 enrichment (falls back to `OPENAI_API_KEY`):

```bash
export OPENAI_API_KEY_UTILS="sk-..."
```

A cold run makes ~11 cached LLM calls (theme, one-word, batched classification,
a 3-vote importance judge, and one `web_search` grounding); chat is one call per
question. **No key? It still runs** — you get the raw, de-duplicated feed
without the GPT layer. Everything tunable lives in
[`ainews/config.py`](ainews/config.py).

## What it does

| Stage | Detail |
|-------|--------|
| **Parallel fetch** | ~30 feeds fetched concurrently (thread pool) with a per-feed timeout + per-feed entry cap; dead feeds are skipped, not fatal. |
| **Disk cache** | Feeds cached 30 min, LLM output 6 h (`~/.cache/ai_news_feed`) — relaunches are instant and don't re-bill. |
| **Loading spinner** | Live progress on every slow phase instead of a silent freeze. |
| **De-dup** | Exact + fuzzy-title de-duplication (`difflib`) collapses the same story across sources. |
| **Theme summary** | One-paragraph "atmosphere" read of the day's headlines. |
| **One-word mood** | A single distilled word for the overall trend. |
| **Classify everything** | Every headline gets a GPT-5.4 sentiment + topic label (batched), so there's no giant "General" bucket. |
| **Color + emoji intelligence** | Each topic cluster gets its own color + emoji; every row carries a colored gutter, a topic chip, and a sentiment glyph (▲ hype / ▼ concern / ● neutral, hollow when read). |
| **TOPIC MIX dashboard** | A heavy-boxed, block-bar chart (`█▉▌`) of the day's topic distribution with rank badges + emoji. |
| **Top-5 leaderboard marquee** | A scrolling ticker that cycles the day's top-5 most important stories (with `①②③` rank badges), chosen by a hybrid score — theme-centrality + cross-source corroboration + recency + source authority — with the #1 re-ranked by a shuffled, majority-vote GPT-5.4 judge. The full ranked list also appears in the `t` overview. |
| **Live grounding** | The #1 story is fact-checked/expanded with GPT-5.4's built-in `web_search` tool, with real clickable citations. |
| **Chat with your feed** | `a` opens a Q&A overlay — ask anything about today's news; answered over the curated, classified day by **gpt-5.4-mini** with live web search + citations (markdown, scrollable, ask follow-ups). |
| **In-app reader** | In the story page, `r` scrapes the full article (via `trafilatura`, as **markdown**) and renders it **colorfully** — headings, **bold**, *italic*, `code`, links, blockquotes, lists — inline & scrollable; `o`/Enter opens it in your real browser — WSL-aware (`explorer.exe`/`wslview`/PowerShell), so links actually open under WSL. |
| **Power tools** | In-app fuzzy search, source filter, bookmarks + read/unread (persisted across runs), and one-key Markdown digest export. |

The whole renderer is **display-width aware** ([`textwidth.py`](textwidth.py)) so
double-width emoji and box glyphs stay aligned, and the loop animates the marquee
on a timer (and rebuilds on terminal resize).

## Keys (in the TUI)

| Key | Action |
|-----|--------|
| `←/→` `h/l` · `Tab` | move focus between TOPICS and STORIES |
| `↑/↓` `j/k` · `PgUp/PgDn` · `g/G` | move within the focused pane |
| `Space` / `Enter` | open the story page → `o`/`Enter` browser · `r` read full article · `↑/↓` scroll · `b` ★ · `←`/`Esc` back |
| `t` | overview: full theme / live context / top-5 leaderboard / topic-mix |
| `a` | chat with your feed (ask a question; `a`/`/` to ask again, `Esc` to close) |
| `/` | fuzzy search (Enter apply, Esc cancel) |
| `f` | filter by source (picker) |
| `b` / `B` | bookmark selected story · show bookmarks only |
| `m` / `u` | mark read/unread · show unread only |
| `e` | export the day's digest to Markdown |
| `c` / `Esc` | clear all filters & search |
| `?` | help overlay |
| `q` | quit (bookmarks + read-state are saved) |

The focused pane has a highlighted border. The header (marquee / banner /
panels) drops gracefully on short terminals so the browser keeps room.
Bookmarks and read-state persist in `~/.cache/ai_news_feed/state.json`; digests
are written to the current directory.

## Configuration

Everything tunable lives in [`config.py`](config.py): the feed list, model id,
per-task reasoning effort / verbosity / token caps, feature flags
(`ENABLE_SENTIMENT`, `ENABLE_CLUSTERING`, `ENABLE_GROUNDING`, …), lookback
window, cache TTLs, and fetch concurrency.

## Architecture

A single `Article` dataclass ([`models.py`](models.py)) is the lingua franca.
Modules are small and single-purpose:

```
config.py         tunables + API-key resolution
models.py         Article dataclass (+ to_dict/from_dict, cluster_size)
cache.py          JSON file cache with per-entry TTL
colors.py         curses color-pair IDs + dynamic topic palette
glyphs.py         geometry + emoji kit (boxes, bars, badges, topic/sentiment glyphs)
textwidth.py      display-width helper (emoji/wide-glyph aware) for alignment
feeds.py          parallel fetch · date filter · fuzzy de-dup (+ corroboration)
analysis.py       pure stats (source counts, top words)
llm.py            defensive Responses-API client (param degradation)
enrich.py         theme / one-word / classify (batched) / web-search grounding
markdown_render.py  rich markdown → colorful color segments
wrapping.py       word wrapping of render lines (width-aware)
importance.py     pick + rank the day's top stories (hybrid score + LLM judge)
state.py          FeedState controller: topic axis / groups / search / filter / bookmarks / leaderboard
persist.py        JSON store for bookmarks + read-state
export.py         Markdown digest writer
ui.py             two-pane curses browser (box primitive, panes, story page, chat, overview)
openurl.py        WSL-aware "open in browser" (explorer.exe / wslview / …)
reader.py         in-app article scraper (trafilatura → bs4 → stdlib fallback)
loading.py        TTY-aware spinner
app.py            orchestration (fetch → enrich → importance → build state → render)
```

The LLM client is deliberately defensive: gpt-5.x parameter names drift between
versions, so any rejected optional parameter (`reasoning`, `text`/`verbosity`,
`max_output_tokens`, `tools`, `include`) is dropped and the call retried — the
app can't hard-fail on an API surface change.
