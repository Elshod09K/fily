# Fily

**A private file organizer for your Mac or Windows PC.** Every night it looks
at the loose files piling up in your Downloads (and any other folders you
choose), works out what each one is, and files it into a sensible folder —
then messages you on Telegram with what it did.

It runs on your own computer with your own free AI key. Nothing is uploaded to
a server you don't control, and it never erases anything.

```
🗂 Organized 14 files
• SAT Prep/ — 6
• Research Papers/ — 4
• Installers/ — 2
🗑 2 duplicates to Trash — 18 MB (Finder → Put Back to undo)

📋 3 need your call
[ 📋 Review 3 ]  [ ↩️ Undo this run ]
```

## What it will never do

- **Erase a file.** Deleting means the Trash (Mac) or Recycle Bin (Windows) —
  you can always put it back.
- **Touch code projects, apps, or photo/music libraries.** Git repos,
  `node_modules`, app bundles, your Photos library and similar are skipped whole.
- **Reorganize folders you already sorted.** Only *loose* files at the top of
  each folder are touched.
- **Move a file you're working on.** Anything modified in the last 24 hours, or
  open in another program, is left alone.
- **Download your cloud files.** OneDrive "online-only" and iCloud-only files
  stay in the cloud; they're skipped rather than fetched.
- **Let the AI decide to delete.** Only an exact byte-for-byte duplicate is ever
  deleted automatically, and the oldest copy is always kept.

Every run can be undone, and if the AI is unreachable, it moves *nothing* and
tells you in the morning.

## What you need

- **A Mac, or a Windows 11 PC**
- **An AI key** — either or both, both are free:
  - **Gemini** (recommended): [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  - **NVIDIA**: [build.nvidia.com](https://build.nvidia.com) → pick any model → *Get API Key*
- **Telegram** (recommended) — you'll create your own private bot during setup;
  it takes a minute

## Install

### Mac

```bash
git clone https://github.com/Elshod09K/fily.git
cd fily
./install.sh
```

Run this in the **Terminal** app.

### Windows

```powershell
git clone https://github.com/Elshod09K/fily.git
cd fily
.\install.cmd
```

No git? On GitHub click **Code → Download ZIP**, extract it, and **double-click
`install.cmd`**. If Windows shows *"Windows protected your PC"*, click **More
info → Run anyway** — that appears for any script downloaded from the internet.

You need **Python 3.11 or newer**. If you don't have it, the installer tells
you how; the quickest is `winget install Python.Python.3.13`.

### Setup

Either way, the installer then runs setup, which asks for:

1. **Your AI key(s)** — each is tested with a real request, and it keeps only
   the models your key can actually use
2. **Your Telegram bot token** — setup walks you through getting one from @BotFather
3. **Which folders** to organize — defaults to Downloads and Desktop
4. **What time** to run each day — defaults to 22:00

Then it schedules itself, checks it can work in your folders, and gives you a
link to connect your phone. **After that you can forget about it.**

> **One possible permission step.**
> **Mac:** a background job can't see your Downloads until you allow it. Setup
> opens the right page and copies the path you need: *System Settings → Privacy &
> Security → Full Disk Access → + → ⌘⇧G → paste.*
> **Windows:** only if *Controlled folder access* is switched on in Windows
> Security (it's off by default). Setup detects it and shows which two programs
> to allow.

## Using it

Everything happens in your Telegram bot:

| | |
|---|---|
| `/status` | recent runs and what's waiting for you |
| `/review` | go through files it wasn't sure about |
| `/run` | organize now instead of waiting |
| `/undo` | put everything from the last run back |
| `/report` | the full report from the last run |
| `/health` | check everything is armed and working |

### Reviewing

Files it isn't confident about wait for you. Each one shows up as a card with
the file's name, size, **a snippet of what's actually inside it**, and a
suggested folder:

```
[ ✅ Move to Research Papers ]
[ 👁 Show in Finder ]    [ 📄 Send me it ]
[ ✏️ Different folder ]  [ ⏭ Skip ]
[ 🗑 Delete ]            [ ✖️ Stop ]
```

**Show in Finder** (or **File Explorer** on Windows) highlights the file on your
computer. **Send me it** uploads that one file to the chat so you can open it
on your phone.

Folder names can be in any language — *Шартномалар* and *Oʻquv reja* work as
well as *Contracts*.

### Duplicates

Byte-identical copies are found locally and the spares go to the Trash / Recycle
Bin — the oldest copy is kept, and both files are re-checked immediately before
deleting. Prefer to decide yourself? Set `duplicates: {action: stage}` in
`config.yaml` and they'll collect in a `_Duplicates/` folder instead.

## Is it working?

| Mac | Windows |
|---|---|
| `.venv/bin/organize health` | `.venv\Scripts\organize health` |

or `/health` in Telegram. It checks the schedule is registered, a run actually
succeeded recently, your folders are readable, the bot is really reaching
Telegram, and your last message was delivered. You don't need to check it
routinely: if no run succeeds for 36 hours, Fily messages you on its own.

### On a laptop

A computer that's asleep runs nothing. A missed run isn't skipped, though — it
happens as soon as the computer wakes.

- **Windows** already asks to wake the PC for the daily run. On battery,
  Windows often only honours "important" wake timers; to allow Fily's (optional):
  ```powershell
  powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
  powercfg /setactive SCHEME_CURRENT
  ```
- **Mac**: schedule a wake a few minutes before the run (needs your password):
  ```bash
  sudo pmset repeat wakeorpoweron MTWRFSU 21:57:00
  ```

## Privacy

| Goes to | What |
|---|---|
| Your AI provider (Gemini/NVIDIA) | file **names**, types, sizes, and up to 800 characters of text from documents — only for files it hasn't seen before |
| Telegram | run summaries and review cards; a whole file **only** when you tap *Send me it* |
| Anywhere else | nothing |

Keys live in `.env` inside the folder, readable only by your user account.
Duplicate detection happens entirely on your computer.

## Updating

```bash
git pull
./install.sh        # Mac
.\install.cmd       # Windows
```

Your keys and settings are kept. To change them: `./install.sh --setup` on a Mac,
`.\install.cmd -Setup` on Windows.

## Uninstalling

| Mac | Windows |
|---|---|
| `.venv/bin/organize install --uninstall` | `.venv\Scripts\organize install --uninstall` |

stops everything; then delete the `fily` folder. Files it already organized stay
where they are.

## Troubleshooting

| | |
|---|---|
| **Bot doesn't reply** | Run `organize doctor` — it tests the connection and shows messages the bot isn't picking up. |
| **Lost the pairing link / new phone** | `organize bot --pair` (or `--unpair` to move to another account) |
| **"… is blocking access"** | The permission step above. |
| **It stopped running** | `organize install` re-arms the schedule. On a Mac, run it from the Terminal app, not an editor's built-in terminal. |
| **Windows: nothing happens on double-click** | Right-click `install.cmd` → *Properties* → tick *Unblock* → OK, then try again. |
| **Wrong folder choices** | Review them; you can also raise `auto_confidence` in `config.yaml` so fewer files move without asking. |

(`organize` means `.venv/bin/organize` on a Mac and `.venv\Scripts\organize` on Windows.)

## Configuration

Setup writes your choices to `config.yaml`. Every other setting comes from
[`config.example.yaml`](config.example.yaml), which documents them all — copy
any setting into your `config.yaml` to change it.

## Command line

Everything the bot does is also available as `organize <command>`: `setup`,
`run` (`--dry-run` to preview), `review`, `undo`, `status`, `health`, `doctor`,
`install`, `bot`, `prune`. Add `--help` to any of them.

## How it works

The AI only ever returns a *label* — a category and a folder name. Plain Python
does all the scanning, moving and deleting, and refuses any folder name that
would land outside the folders you chose. So even a file crafted to trick the
AI can at worst produce an oddly named folder. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Tests are hermetic: no network, no real Trash, no scheduler changes. CI runs
them on real Windows and macOS machines with Python 3.11 and 3.13, and on
Windows also registers and runs real scheduled tasks.

## License

[MIT](LICENSE)
