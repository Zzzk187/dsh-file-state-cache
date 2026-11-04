# dsh-file-state-cache

**English** · [简体中文](README.zh-CN.md)

> A DSH skill that makes file reading **stateful** — so the agent stops re-reading what it already read.

`ls` → `grep` → `read` → `read again to confirm the edit`. Four round-trips, three of which are the agent asking itself *"didn't I already look at this?"*. This skill replaces that with one exploration call, reads that remember they happened, and edits that return a diff instead of demanding a full re-read.

---

## What it actually saves

| Waste | Before | With this skill |
|---|---|---|
| **Exploration round-trips** | `ls`, then `grep`, then a blind `read` — three turns | one `explore "keyword"` → ranked hit files (with the **definition line number**) + hit lines + signatures + snippets + the next command to run, ~1.5 KB |
| **Re-reading unchanged files** | full file again, every time | one line: *"unchanged, read N times before, last range …"* |
| **Verifying an edit** | re-read the whole file | the unified diff + the **exact** changed line range |
| **Reading N files to understand a structure** | N reads | one `digest` → N signatures in one screen |

The state lives **on disk**, not in the agent's context. That is the whole trick: it survives across tool calls, turns, and restarts. A plain tool invocation cannot do that, which is why this is a CLI plus a skill rather than a set of registered tools.

**Measured** on a 115-file TypeScript project over 538 real queries: `explore` averages 1.4 KB, puts the answer file first 93.6% of the time and in the top three 100% of the time. Touching the same file 4 times in a session saves 65%; editing N places with one confirmation each saves a steady 88%. But for a **one-shot task where you read a file once**, it loses — see [when not to use it](#when-not-to-use-it).

---

## Install

```bash
dsh plugin --profile web add Zzzk187/dsh-file-state-cache
```

Or from a local checkout:

```bash
dsh plugin --profile web add /path/to/dsh-file-state-cache
```

**Requirements:** Python 3.8+. Zero third-party packages — standard library only.

The skill appears in your skill catalog as `file-state-cache`.

---

## Use

Once loaded, the skill tells the agent where its script lives. Manually:

```bash
F="<package-root>/scripts/fstate"

"$F" explore "handleRequest" --root .     # locate code: overview + hits + signatures
"$F" read src/app.py                      # 1st time: full text. 2nd time: one line.
"$F" read src/app.py --mode range --start 40 --end 90
"$F" edit src/app.py --old 'return 1' --new 'return 2' --reason "fix the boundary"
"$F" digest src/                          # structure of a whole directory at once
"$F" state src/app.py                     # version, line count, read count, STALE?
"$F" hot --n 10                           # which files am I re-reading too much?
```

If the launcher can't find Python, pin it:

```bash
export FSTATE_PYTHON=/usr/bin/python3
```

### `read --mode auto` (the default) decides for you

| Disk state | What you get back |
|---|---|
| never read | the full text (paginated if huge) |
| read before, unchanged | **a single summary line** — no content |
| read before, changed on disk | **only the diff** + exact changed line range |

### Example: what an edit returns

```
# 已改 src/app.py  → v2  19 行  区间 14..14
--- a/app.py
+++ b/app.py
@@ -11,7 +11,7 @@
-    return {"ok": True, "uptime": 0}
+    return {"ok": True, "uptime": 42}
[确认改动不必重读全文：看局部用 --mode range --start 14 --end 14]
```

That diff *is* the verification. The changed range (`14..14`) comes from `SequenceMatcher`, not from the diff hunk header — so it excludes the three lines of context and points at the one line that actually moved.

---

## Subcommands

| Command | What it does |
|---|---|
| `explore QUERY` | directory overview + ranked hit files + hit lines + signatures + snippets + next step |
| `read PATH` | cached read: unchanged → summary, changed → diff, `--mode range` for a slice |
| `edit PATH --old/--new` | edit and return a unified diff + exact changed line range |
| `write PATH --content-file` | whole-file write (preview when new, diff when overwriting) |
| `digest PATH...` | structure of many files (line counts + signatures + cache state) |
| `state PATH` | version / lines / read count / last edit range / STALE flag |
| `diff PATH` | the last diff |
| `reset PATH` | drop one file's cache |
| `hot [--n N]` | files you have read the most — a self-check for re-reading habits |

`"$F" --help` lists everything.

---

## Design notes

- **Disk is the source of truth.** Every read and edit compares SHA-1 first. Edit the file in your editor, `git checkout`, whatever — the next `read` notices and hands you a diff instead of stale content. `state` reports `STALE`.
- **Shadow copies.** Files up to 2 MB get a `.shadow` copy so a later diff needs no git. Every state file is `<sha1(path)[:20]>.json` under `~/.dsh/fstate-cache/`.
- **Encoding.** A real BOM (UTF-8 / UTF-16 / UTF-32, either endianness) decides the encoding up front; only when there is no BOM are the fallbacks `utf-8` → `gbk` → `latin-1` tried. Write-back preserves the original BOM, encoding and newline style — a file that had no BOM never gains one. Everything over 8 MB, or containing a NUL byte, is refused as "not text" instead of being mangled. `utf-8-sig` is deliberately *not* used as a probe: it happily decodes plain UTF-8, which is how earlier versions silently stamped `EF BB BF` onto every file they edited.
- **Newlines are tolerant.** An LF fragment applied to a CRLF file matches; the file keeps its CRLF style on write-back. `write` normalizes the replacement body to the target file's newline style, so a whole-file overwrite cannot silently convert CRLF to LF either.
- **No silent parameter surprises.** `edit` refuses to run without an explicit `--new` / `--new-file` (an omitted `--new` used to mean "replace with empty" and still print *edited*); deleting requires writing `--new ''` on purpose. Argument prefix abbreviations are disabled on the top-level parser *and* on all nine subparsers, because `--content hi` used to be read as `--content-file hi` and `--old abc` as "open the file named abc". Supplying identical `--old` and `--new` reports *no change* instead of bumping a version. Every failure path (missing `--old-file`, read-only target, unwritable file) prints one actionable sentence and leaves the file untouched, rather than dumping a traceback.
- **Bounded output — and it always says what it cut.** Long diffs and logs spill to a file and you get a preview plus the path, so output never blows up the context window. Whenever a cap fires (`--max-files`, `--max-scan`, `hot --n`, `--max-sigs`), a line like `remaining N` or `scanned the first N files and stopped (-max-scan)` is printed. Don't skim past that line: the answer may be in the part that got cut. Silent truncation never errors and never computes anything wrong — it just makes you believe "that's all there is", which is exactly the failure mode this tool exists to prevent.
- **Cache location is yours.** `FSTATE_HOME` moves it. `reset <path>` drops one file; `hot` shows what's accumulating.

## When NOT to use it

- You already know the exact line numbers → a native read tool is faster; caching is overhead.
- One-shot scripts, single commands → nothing happens twice, so a cache earns nothing.
- Binary, images, or files over 8 MB → it refuses them by design.

To be honest about it: **for a single-pass flow ("locate the problem → read a chunk → edit one place → confirm"), this skill actually costs more** — measured 11276 → 13140 B·turn (16.5% worse), because `explore` is ~18× more expensive than a precise `grep`, and `read` adds a line-number prefix that bare `sed` doesn't. That prefix is deliberate (the agent needs it to name an exact edit range), so it is not being trimmed for byte count.

It pays off when **the same content gets reused** across the session: touching a file 4 times saves 65%, editing N places with one confirmation each saves 88%, and understanding 20 files at once saves 80%.

**Rule of thumb: will this happen a second time? Yes → use it. No → use the native tool.**

## Compatibility

Verified against DSH `0.2.1-rc.5`. The skill provider uses `ctx.skills.registerProvider` and degrades gracefully on hosts that don't pass a `control` object (SKILL.md edits then need a restart instead of hot-reloading).

The skill registers at rank `600` (bundled). If you also keep a local copy under `~/.dsh/dsh-skill/file-state-cache`, the local one wins — a plugin should not override your own specialized version.

## License

MIT
