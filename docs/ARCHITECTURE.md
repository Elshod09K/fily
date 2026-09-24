# How Fily works

The design goal is one sentence: **a bad night is a no-op, never a loss.** Fily
runs unattended against a real home directory, so every decision has to be
reversible, nothing is ever erased, and no failure may be silent.

## Shape

A Python CLI, not an agent loop. Scanning, hashing, moving and deleting are
deterministic Python. The AI is called once per batch of ~20 files, purely to
classify, and returns labels.

```
organizer/
├── cli.py          entry point: setup, run, review, undo, health, doctor, …
├── setup.py        the one-time wizard; validates everything live
├── launchd.py      builds and registers the three jobs
├── config.py       config.yaml layered over config.example.yaml
├── safety.py       hard rules — not configurable
├── scanner.py      walks the folders, applies exclusions
├── extract.py      text excerpts from pdf/docx/txt, locally
├── dedupe.py       size → head/tail signature → sha256, locally
├── classify.py     prompt building, batching, output validation
├── planner.py      labels → validated moves; auto vs. review
├── applier.py      moves, trashes, journals; reverses runs
├── trash.py        deleting via the macOS Trash, with re-verification
├── journal.py      append-only move log + sha256 decision cache
├── providers/      Gemini, NVIDIA, the failover chain, model probing
├── bot.py          the Telegram front end
├── telegram.py     stdlib-only Bot API client
├── notify.py       desktop + Telegram delivery, deferred failure alerts
└── health.py       positive checks that it is actually running
```

### A run

1. **Scan** — loose files at the top of each folder; exclusions applied.
2. **Extract** — up to 800 characters of text from documents; images get
   name and metadata only.
3. **Dedupe** — exact duplicates found by hash. They never reach the AI.
4. **Cache** — a file whose sha256 was classified before reuses that decision,
   so a steady-state night classifies only genuinely new files.
5. **Classify** — batches go down the provider chain.
6. **Plan** — every label validated; each file goes to auto-apply or review.
7. **Apply** — moves and deletions journaled *before* they are considered done.
8. **Report** — Markdown report, desktop notification, Telegram message.

### Three launchd jobs

| Job | When | Does |
|---|---|---|
| `local.fily.run` | daily, at the configured time | the run above |
| `local.fily.alert` | daily, morning | delivers overnight failures; watchdog |
| `local.fily.bot` | always (`KeepAlive`) | Telegram front end |

Plists are generated at install time from wherever the copy lives, so the
repository holds no machine paths. **They hold no secrets either** — a
LaunchAgent plist is world-readable and gets backed up — so keys are read from
the project's `chmod 600` `.env` by the app itself.

`launchctl` registers jobs into the domain of whatever process calls it.
Bootstrapping from a sandboxed or short-lived process (an IDE's terminal, an
automation tool) produces jobs that **vanish without a trace** when it exits.
That is why install instructions insist on the Terminal app, and why the
health check looks for positive signs of life rather than an absence of errors.

## Safety rules

In `safety.py`. Not configurable.

**Never touched:** `~/Library`, `~/.Trash`, `~/Applications`, system folders,
anything hidden, symlinks, hardlinked files, partial downloads, empty files,
files modified in the last 24 hours, files another process has open.

**Atomic — never descended into:** any directory containing `.git`,
`node_modules`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`,
`.venv`, `SKILL.md` and similar; and macOS package bundles (`.app`,
`.photoslibrary`, `.musiclibrary`, `.rtfd`, …), which look like folders but
are single documents. Descending into one is how a Photos library gets
corrupted.

**`~/Pictures`, `~/Movies`, `~/Music`** are destinations only. On most Macs
they hold nothing but Apple-managed libraries.

**Only loose files.** With `scan_depth: 1`, anything already inside a subfolder
is by definition organized, and existing folders — including extracted
archives and tool directories — are left intact.

### The AI cannot cause an unsafe write

File names and document text are **untrusted input**: a PDF can contain text
aimed at the model. The model's output is constrained to:

```json
{"file_id": 17, "category": "academic-paper", "folder": "Research/Papers",
 "confidence": 0.93, "reason": "…"}
```

`category` must be in a fixed enum. `folder` must be relative, at most three
segments, from a restricted character set, with no `..`, no leading `/` or `~`,
and no bundle suffix. The resolved parent — symlinks followed — must land inside
a folder the user chose. Anything failing that goes to review. The worst a
successful prompt injection achieves is an oddly named folder in a place the
user already asked Fily to organize.

**The model never decides to delete.** Only a local byte-for-byte hash match
does, or the user does.

### Deleting

Always via the macOS Trash (`send2trash`), never `unlink`. Before a duplicate is
trashed, both it and the copy being kept are re-hashed *at that moment*; if the
keeper has vanished or either file changed since the scan, nothing is deleted.
The oldest copy is always the one kept.

macOS protects `~/.Trash` under TCC: a process without Full Disk Access can put
files there but cannot list them back. So `organize undo` can un-delete only
with Full Disk Access; otherwise it says so and points at Finder's Put Back,
which always works.

### Blast radius

- More than `max_moves_per_run` planned moves → apply nothing, queue everything.
- A wall-clock budget, checked between batches, so a hung provider can't run
  until morning. Already-classified files stay valid.
- A single-holder run lock, so `/run` and the scheduled job never overlap.
- Documents over 25 MB are classified by name and type alone, and any one text
  extraction is capped at 20 seconds — one pathological PDF can otherwise
  stall a whole run.

### Undo

Every move is appended to `state/journal/<run-id>.jsonl` **before** the rename,
so an interrupted run is still reversible. Each entry records how the file's
content was fingerprinted — full sha256, or size + head/tail for files over
256 MB — so undo verifies with the same method it recorded with. Undo refuses to
restore a file whose content changed since the move.

## Providers

Tried strictly in order. A typical chain written by setup:

```
gemini  primary      ×5
nvidia  model A      ×5
nvidia  model B      ×5   (rotate model, same key)
gemini  secondary    ×3
→ all failed: move nothing, queue a morning alert
```

Timeouts, 429s, 5xx and unparseable JSON are retried with exponential backoff
and jitter. A rejected key (401/403) or a dead model (404/410) skips straight to
the next link rather than burning attempts.

**A provider's model list is not evidence.** On one NVIDIA key tested during
development, ten of twelve listed models answered 404 (not entitled) or 410
(retired). So setup sends a small real request to each candidate and keeps only
the ones that answer, and `organize doctor` re-probes the configured chain.

Newer Gemini models reason by default. Sorting files is recall, not reasoning,
and leaving it on made a 20-file batch take minutes instead of seconds, so each
request asks for the lowest thinking setting the model accepts.

## Telegram

`telegram.py` uses only the standard library, with an explicit `certifi` CA
bundle: a python.org install may ship without a usable default bundle, which
makes `urllib` fail TLS verification while `curl` and httpx-based SDKs work —
an asymmetry that hides the problem well.

**Reads fail loudly, writes fail soft.** Polling raises on error, because a
poller that returns "no messages" on failure is indistinguishable from a quiet
day. Sending never raises, because a dead network must not break a run — and
every send is logged, so delivery is checkable rather than assumed.

**Pairing.** The bot answers exactly one chat. Setup creates a one-time code and
prints a `t.me/<bot>?start=<code>` link; only a `/start` carrying that code can
claim the bot, and the code is burned on use. A bot username alone is not
enough.

The bot exits cleanly when its own source files change, and `KeepAlive` brings
it straight back on the new code — otherwise edits silently don't take effect.

## Health

Built after two outages that shared one shape: nothing crashed, nothing was
logged, it just stopped. Absence of errors is not evidence of working, so
`organize health` checks for positive signs:

- each launchd job registered (and the bot actually running)
- a run *succeeded* recently
- every folder readable — `os.walk` swallows permission errors and yields
  nothing, so a folder macOS hides looks exactly like an empty one
- Telegram paired, and the last message delivered
- whether sleep will delay the next run

The morning job doubles as a watchdog: no successful run in 36 hours → a
Telegram message, unprompted.

## State

All in `state/`, gitignored:

| | |
|---|---|
| `journal/*.jsonl` | every move and deletion, for undo; kept 90 days |
| `cache.db` | sha256 → decision; each file is classified once, ever |
| `runs/*.md` | human-readable reports |
| `review_queue.json` | what's waiting, with text excerpts |
| `pending_alert.json` | an undelivered overnight failure |
| `telegram.json` | the paired chat (mode 600) |
| `logs/` | job output, and `telegram.log` of every delivery |

## Known limits

- Duplicate detection is byte-exact. Two exports of the same document that
  differ by an embedded timestamp aren't grouped, and are never auto-deleted.
- Images are classified by name and metadata; there's no vision pass. Unclear
  images land in review, where *Send me it* shows them.
- The first batch of a run has no context from others; later batches are shown
  the folders earlier ones chose, which keeps a project together.
- macOS only.
