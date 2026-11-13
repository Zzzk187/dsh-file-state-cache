# dsh-file-state-cache · File State Cache

**English** · [简体中文](README.md)

> Makes the agent remember "I already read this file" instead of re-reading it every time.

A DSH plugin + skill. Once installed, the agent's file reading becomes **stateful**.

---

## What it saves you

Agents often go in circles: `list the directory → grep a keyword → open the file → open it again to confirm the edit`. Most of that is repeat work. With this skill:

| Before | After |
|---|---|
| several turns just to find code | one `explore "keyword"` tells you the file, the line, and what to read next |
| full file again, every time | second time: one line saying "unchanged" |
| re-read the whole file to verify an edit | `edit` returns the diff + the line that changed |
| read 10 files to understand a directory | one `digest` lists the function signatures |

What you save is **context (tokens)**. What it costs is a little disk.

> **One-line rule**: will this happen a second time? Yes → use it. No → use the native read tool.
> Note it saves tokens, **not time** — every call still starts a small process.

---

## Install

```bash
dsh plugin --profile web add Zzzk187/dsh-file-state-cache
```

- **Windows 10 / 11** (this project targets Windows only; Linux / macOS are out of scope)
- Requires **Python 3.8+**. No third-party packages.
- The skill shows up in your catalog as `file-state-cache`.

---

## 30-second start

```bash
F="<package-root>/scripts/fstate"          # the skill tells you where it lives
"$F" explore "handleRequest" --root .      # find code
"$F" read src/app.py                       # 1st time: full text. 2nd: "unchanged"
"$F" edit src/app.py --old 'return 1' --new 'return 2'
"$F" digest src/                           # structure of a directory
```

`<package-root>` is usually `~/.dsh/profiles/<profile>/node_modules/dsh-file-state-cache`. You've got the right one when `python "<package-root>/scripts/fstate.py" -h` works.

`read` decides by itself what to hand you:

| Situation | What you get |
|---|---|
| first read | the full text |
| read before, unchanged, and this session already got the full text | one line "unchanged" (no body) |
| read before, changed on disk | only the diff + the changed line numbers |

`edit` hands you the diff right away — **that diff is the verification, no re-read needed**.

---

## Commands

| Command | What it does |
|---|---|
| `explore "keyword"` | find where code lives |
| `read FILE` | cached read; unchanged → summary, changed → diff |
| `read FILE --mode range --start 40 --end 90` | read just a slice |
| `edit FILE --old X --new Y` | change one spot, get the diff |
| `write FILE --content-file BODY` | whole-file write |
| `digest DIR/` | structure of many files |
| `state FILE` | cache state for this file |
| `diff FILE` | replay the last full diff |
| `reset FILE` | drop this file's cache |
| `hot` | which files am I re-reading? |

`--help` lists everything. The switches you'll actually use:

- `--mode full`: explicitly ask for the whole file
- `--brief` on `explore` / `digest` (since 1.1.1): keep only file + hit lines + the next command; drops decorative output. Locators and every ⚠ warning are kept. Without the switch, output is byte-for-byte identical to older versions.
- `--label "name"`: show a short name instead of the long path

---

## Two things to know

1. **Big files don't arrive in one go.** Default cap is 400 lines / 20000 characters; when it cuts, it **says so explicitly ("not counted as read")** and tells you how to continue — it will never fob you off with "unchanged". To actually hand the whole file to the Agent, write `read FILE --mode full --max-chars 100000`.
2. **"Unchanged" only counts within one session.** A changed `DSH_SESSION_ID` gets the full text again. When in doubt, run `reset`. Deliberate: saving context must not turn into "making you think you already saw it".

How does it detect changes? It checks the file's fingerprint on disk before every read; if it differs, the file is treated as changed. If even the previous-text backup is gone, it says honestly "I have no baseline" and gives you the full text — **it never pretends to know what changed**.

---

## FAQ

**Will it hand me stale content?** No — the fingerprint is checked before every read.

**Will it corrupt my files?** No. BOM, encoding (UTF-8 / GBK / …) and newlines (CRLF / LF) are preserved as-is; a failed write rolls back rather than leaving a half-written file.

**Cache is dirty / I want to start over?** `reset FILE` clears one; deleting `~/.dsh/fstate-cache/` clears everything (harmless, rebuilt on demand; `FSTATE_HOME` moves it).

**How much disk?** A few hundred bytes per file plus a backup of the text (≤ 2 MB, only for files actually read).

**Large files / binaries?** Files over 8 MB and binaries are refused by design — use the native tools.

---

## Troubleshooting (the common ones)

| You see | What to do |
|---|---|
| `未找到 old 片段` | `--old` doesn't match (spaces / indentation); copy the lines verbatim via `read --mode range` |
| `old 片段出现 N 次，不唯一` | add a line of context, or `--all` if you really mean all |
| `找不到 python` | set `FSTATE_PYTHON=/path/to/python` |

Exit codes: **0** success, **2** failure, **3** `edit` fragment not found / not unique, **130** interrupted (Ctrl-C).

---

## Tests

```bash
python -m pytest tests/ -q
```

94 cases covering cache correctness, encoding & newline fidelity, concurrency, crash recovery and Windows file locking; green on both Python 3.8 and 3.13 under Windows (pytest is **test-time only; the runtime has zero third-party dependencies**).

---

## More

- Full usage, when it's worth it (measured numbers), and the gotcha list: [SKILL.md](SKILL.md)
- Version history, benchmark numbers, known limits: [CHANGELOG.md](CHANGELOG.md)
- Verified against DSH `0.2.1-rc.5`

MIT · see [LICENSE](LICENSE)
