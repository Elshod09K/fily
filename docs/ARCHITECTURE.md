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
├── host/           everything that differs between macOS and Windows
├── launchd.py      the three jobs, on macOS
├── winsched.py     the three jobs, on Windows (Task Scheduler)
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

1. **Scan** — every level of every chosen folder, with exclusions applied;
   each subfolder is summarized (files, size, newest change, a content
   fingerprint).
2. **Judge folders** — each subfolder is a *set*, a *category* or a *dump*
   (see below). Nothing inside a set is looked at.
3. **Extract** — up to 800 characters of text from documents; images get
   name and metadata only.
4. **Dedupe** — exact duplicates found by hash. They never reach the AI.
5. **Remember** — a file Fily (or you, through review) already placed is
   never re-sorted, and a file whose sha256 was classified before reuses that
   decision, so a steady-state night asks the AI about nothing old.
6. **Classify** — batches go down the provider chain.
7. **Plan** — every label validated; each file goes to auto-apply or review.
8. **Apply** — whole sets first, then files; every move and deletion is
   journaled *before* it is considered done.
9. **Report** — Markdown report, desktop notification, Telegram message.

### Three scheduled jobs

| Job | When | Does |
|---|---|---|
| `run` | daily, at the configured time | the run above |
| `alert` | daily, morning | delivers overnight failures; watchdog |
| `bot` | always | Telegram front end |

On macOS they're launchd agents (`local.fily.*`); on Windows, tasks in a
*Fily* folder of Task Scheduler, created per-user with no admin rights.
Either way the definitions are generated at install time from wherever the
copy lives, so the repository holds no machine paths — and **no secrets**: a
LaunchAgent plist is world-readable and gets backed up, so keys are read from
the project's owner-only `.env` by the app itself.

Task Scheduler's defaults are wrong for a laptop, and each would fail
silently, so they are overridden: tasks start and keep running on battery
(`DisallowStartIfOnBatteries`/`StopIfGoingOnBatteries` off), a run missed
while the PC was off or asleep is caught up (`StartWhenAvailable`, which
launchd does by default), and the daily run may wake the PC (`WakeToRun`).
The bot task starts at logon, restarts on failure, and has a 5-minute
repeating trigger with `IgnoreNew` as a safety net. Tasks run `pythonw.exe`
(no console window) with `-X utf8`, logging to `state/logs/` because pythonw
has no stdout at all.

On macOS, `launchctl` registers jobs into the domain of whatever process calls it.
Bootstrapping from a sandboxed or short-lived process (an IDE's terminal, an
automation tool) produces jobs that **vanish without a trace** when it exits.
That is why install instructions insist on the Terminal app, and why the
health check looks for positive signs of life rather than an absence of errors.

## The host layer

`organizer/host/` is the only place that knows which OS it's on: protected
folders, the file manager, notifications, the Trash, file locking, process
checks, known-folder locations, power settings and permission fixes. The rest
of the code calls `host.reveal(path)` or `host.is_file_open(path)` and never
branches on the platform.

The Windows implementation makes every Win32 call lazily, so it imports — and
its parsers and XML are unit-tested — on any OS; the Win32 calls themselves run
on the Windows CI job. A few Windows specifics worth knowing:

- **Known folders.** OneDrive usually moves Desktop, Documents and Pictures
  under `~/OneDrive`, so `~/Desktop` is often not the real Desktop. Paths like
  `~/Desktop` are resolved with `SHGetKnownFolderPath`, which also maps a
  Mac-style `~/Movies` to Videos.
- **Process checks.** `os.kill(pid, 0)` checks for a process on POSIX but
  *terminates* it on Windows; the run lock uses `OpenProcess` instead.
- **Busy files.** Where macOS uses `lsof`, Windows tries an exclusive open and
  looks for a sharing violation. It needs read access, since Windows skips
  sharing checks for attribute-only opens, and passes `FILE_FLAG_OPEN_NO_RECALL`
  so the check itself never downloads a cloud file.
- **Timeouts.** There is no `SIGALRM`, so a stuck PDF parser is abandoned on a
  daemon thread (not a pool thread, which would be joined at exit and hang
  the run).
- **Encoding.** Python on Windows reads and writes text in the ANSI code page
  (cp1252) unless told otherwise, which fails on Cyrillic or Uzbek names. Every
  file operation states UTF-8 explicitly, and CI runs Windows with UTF-8 mode
  off so a missed one fails there.
- **Localized output.** Command output (`powercfg`, `icacls`, `schtasks`) is
  translated on non-English Windows, so parsers match on values and structure,
  never on words; task status comes from PowerShell objects as JSON.
- **Secrets.** `chmod 600` means nothing on Windows; `.env` and the pairing
  files have inheritance removed and a single full-control entry for the
  user's SID.

## Safety rules

In `safety.py`. Not configurable.

**Never touched:** system folders (`~/Library` and friends on a Mac; Windows,
Program Files, ProgramData and `AppData` on Windows), the Trash, a whole drive,
anything hidden (dotfiles, and the hidden/system attributes on Windows),
symlinks and junctions, hardlinked files, shortcuts (`.lnk`, `.url`), partial
downloads, empty files, files modified in the last 24 hours, files another
process has open, and **cloud-only files** — OneDrive "online-only"
placeholders and iCloud "dataless" files, which any read would download.

**Atomic — never descended into:** any directory containing `.git`,
`node_modules`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`,
`.venv`, `SKILL.md` and similar; and macOS package bundles (`.app`,
`.photoslibrary`, `.musiclibrary`, `.rtfd`, …), which look like folders but
are single documents. Descending into one is how a Photos library gets
corrupted.

**Pictures, Movies/Videos and Music** are destinations only: loose media is
routed into them, and on most Macs they hold nothing but Apple-managed
libraries.

## Subfolders

Looking inside subfolders is where an organizer can do the most damage: pull a
file out of a folder you arranged on purpose, or scatter the pieces of
something that only makes sense together. So every subfolder is judged as a
whole before anything inside it is touched (`triage.py`):

| Kind | Meaning | What happens |
|---|---|---|
| **set** | the files belong together: an extracted download, an exam pack, a project, one month's documents | filed as one piece, or left alone; nothing inside is examined or deleted |
| **category** | an organizing folder for one kind of thing | looked inside; a file that fits stays |
| **dump** | a catch-all with no theme | looked inside and sorted out; never offered as a destination |

Judgements go **top-down**, so a set's subfolders are never even asked about.
Folders Fily created are categories without asking; folders it already filed
as a set stay put. An unanswered or unrecognized judgement means **set**:
keeping a folder together is always safe, splitting one is not. Category and
dump verdicts are remembered by location (a category stays a category as files
come and go); set verdicts by content fingerprint, so adding a file to a set
gets it looked at again.

Stability rules, so folders don't churn night after night:

- **Placed means placed.** Every move — automatic or chosen in review — is
  remembered (`placements` in `cache.db`, backfilled once from older journals).
  A placed file is never re-sorted, even if you later moved it yourself. Only
  a *loose* copy of a placed file is looked at again, as a possible duplicate.
- **Already fitting means staying.** A nested file's prompt says where it is;
  if the AI answers with that same folder, nothing happens and nothing is
  queued.
- **Pulling a file out of a category needs near-certainty** (0.95); below
  that it goes to review. Files in a dump, and loose files, use the normal
  rules. A set loose at the top moves at the normal threshold; one already
  inside a folder needs near-certainty too.
- **A set never moves into itself or into another set**, and files are never
  filed into a set's insides (its top level is fine). A file headed into a set
  that moves in the same run follows it; if the set's move fails, so does the
  file's.

Moving a set is a single `rename` — same volume, atomic, never a copy. It is
refused if any file inside is open or changed within the quarantine window,
journaled first with a manifest of its contents, and undone only if the
contents still match exactly.

`scan_depth: 1` restores the original behaviour: only loose files at the top
of each folder, no folder judgements.

### The AI cannot cause an unsafe write

File names and document text are **untrusted input**: a PDF can contain text
aimed at the model. The model's output is constrained to:

```json
{"file_id": 17, "category": "academic-paper", "folder": "Research/Papers",
 "confidence": 0.93, "reason": "…"}
```

`category` must be in a fixed enum. `folder` must be relative, at most three
segments, with no `..`, no leading `/` or `~`, no bundle suffix and no name
Windows reserves (`CON`, `LPT1`, …). Each segment may contain letters and
digits from any script — folders can be named in Uzbek or Russian — plus space
and `_ . - & ( ) '`, and nothing else: control and format characters
(including zero-width and right-to-left overrides, which disguise names) and
look-alike slashes such as U+2215 are rejected by Unicode category. Names are
normalized to NFC so one folder can't exist under two spellings. The resolved parent — symlinks followed — must land inside
a folder the user chose. Anything failing that goes to review. The worst a
successful prompt injection achieves is an oddly named folder in a place the
user already asked Fily to organize.

**The model never decides to delete.** Only a local byte-for-byte hash match
does, or the user does.

### Deleting

Always via the Trash or Recycle Bin (`send2trash`), never `unlink`. Before a duplicate is
trashed, both it and the copy being kept are re-hashed *at that moment*; if the
keeper has vanished or either file changed since the scan, nothing is deleted.
The oldest copy is always the one kept.

macOS protects `~/.Trash` under TCC: a process without Full Disk Access can put
files there but cannot list them back. So `organize undo` can un-delete only
with Full Disk Access; otherwise it says so and points at Finder's Put Back,
which always works. Windows has no single readable Recycle Bin folder, so there
recovery always goes through Explorer's *Restore*.

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

The bot exits (with code 75) when its own source files change, and the
scheduler brings it straight back on the new code — otherwise edits silently
don't take effect. Non-zero because Task Scheduler only restarts a task that
failed. It retries an unreachable Telegram with backoff rather than exiting,
timestamps every log line, and records a heartbeat after each successful poll.

## Health

Built after two outages that shared one shape: nothing crashed, nothing was
logged, it just stopped. Absence of errors is not evidence of working, so
`organize health` checks for positive signs:

- each scheduled job registered (and the bot actually running)
- the bot's heartbeat is recent — a running process that can't reach
  Telegram is still a live process
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
- On Windows, a move that would exceed the 260-character path limit fails
  cleanly and is reported, unless long paths are enabled in Windows.
