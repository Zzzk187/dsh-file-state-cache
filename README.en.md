# dsh-file-state-cache · File State Cache

**English** · [简体中文](README.md)

> Makes the agent remember "I already read this file" instead of re-reading it every time.

A DSH plugin + skill. Once installed, the agent's file reading becomes **stateful**.

---

## What it solves

Agents waste turns going in circles:

```
ls to see the directory -> grep to find a keyword -> read the file -> read it again to confirm the edit
```

Three of those four steps are redundant work. With this skill:

| Before | After |
|---|---|
| `ls` + `grep` + a blind `read` — three turns | one `explore "keyword"` tells you which file, which line is the definition, and what to read next |
| full file again, every time | second time: one line saying "unchanged" |
| re-read the whole file to verify an edit | `edit` returns the diff + the exact line that changed |
| read 10 files to understand a structure | one `digest` gives 10 signatures on a screen |

What you save is **context (tokens)**. What it costs is a little disk.

> **One-line rule**: will this happen a second time? Yes → use it. No → use the native read tool.

---

## Install

```bash
dsh plugin --profile web add Zzzk187/dsh-file-state-cache
```

- Requires **Python 3.8+**. No third-party packages — standard library only.
- The skill appears in your catalog as `file-state-cache`.
- From a local checkout: `dsh plugin --profile web add /path/to/dsh-file-state-cache`

---

## 30-second start

```bash
F="<package-root>/scripts/fstate"        # the skill tells you where it lives
```

`<package-root>` is the installed plugin directory, usually:

```
~/.dsh/profiles/<profile>/node_modules/dsh-file-state-cache
```

(On Windows: `C:\Users\you\.dsh\profiles\<profile>\node_modules\dsh-file-state-cache`. If unsure, you have the right one when `python "<package-root>/scripts/fstate.py" -h` runs.)

```bash
"$F" explore "handleRequest" --root .    # when you don't know where the code is
"$F" read src/app.py                     # 1st time: full text. 2nd: one line.
"$F" edit src/app.py --old 'return 1' --new 'return 2'
"$F" digest src/                         # structure of a whole directory at once
```

`read` decides by itself what to hand you:

| Disk state | What you get |
|---|---|
| never read | the body |
| read before, unchanged, and **this session** already got the full body | a one-line summary — **no body** |
| read before, changed on disk | **only the diff** + the changed line numbers |

> Two things that trip up newcomers:
> - **At most 400 lines per read.** A bigger file gets cut, and it says so — "已显示前 400/1000 行，**不算读过**" plus the command to continue. The next `read` hands you more; it will not fob you off with "unchanged".
> - The summary only holds **inside the current session**. A new session, or a compacted context, gets the body again. That is deliberate: saving context must not turn into "making you think you already saw it".

An `edit` looks like this — **that diff is the verification, no re-read needed**:

```
# 已改 src/app.py  → v2  19 行  区间 14..14
--- a/app.py
+++ b/app.py
@@ -11,7 +11,7 @@
-    return {"ok": True, "uptime": 0}
+    return {"ok": True, "uptime": 42}
```

`区间 14..14` points at the one line that actually moved — it excludes the three lines of diff context.

---

## Commands

| Command | What it does |
|---|---|
| `explore "keyword"` | find code: hit files + definition line numbers + snippets + next command |
| `read FILE` | cached read: unchanged → summary, changed → diff |
| `read FILE --mode range --start 40 --end 90` | read just a slice |
| `edit FILE --old X --new Y` | change one spot, get the diff + exact line range |
| `write FILE --content-file BODY` | whole-file write |
| `digest DIR/` | structure of many files at once |
| `state FILE` | cache state for this file (version, read count, stale?) |
| `diff FILE` | replay the last full diff |
| `reset FILE` | drop this file's cache |
| `hot` | which files am I re-reading? (self-check) |

`--help` lists everything.

**Useful switches:**

- `--mode full` / `range` / `changed` — say what you want instead of letting it decide
- `--max-chars 20000` — output cap; when it cuts, **it always tells you**
- `--label "config parser"` — show that name instead of the long path
- `write --mkdir` — create missing parent directories (off by default, so a typo'd path fails loudly instead of silently creating a tree)

---

## How it remembers

**The state lives on disk, not in the context** — which is why it survives across tool calls, turns and restarts.

One JSON per file under `~/.dsh/fstate-cache/`, plus a `.shadow` copy of the text as of the last read (files ≤ 2 MB only) so a diff needs no git.

```
read(FILE)
  +- SHA-1 of what's on disk right now
  +- no record ------------> full text (first time)
  +- SHA-1 unchanged ------> one-line summary (unchanged)
  +- SHA-1 changed --------> shadow available? -> diff + exact line range
                            no shadow?         -> says so honestly, full text
```

That last branch is the point: **it will not pretend to know what changed.**

> **A deliberate limit**: the "I already delivered this body" receipt is valid **only within the current session**. After a session change or a context compaction, `read` hands you the body again instead of dismissing you with "unchanged". A tool that saves context must never manufacture the illusion of having seen something.

---

## Design decisions worth knowing

- **Encoding is never changed behind your back.** A real BOM (UTF-8/16/32, either endianness) decides the encoding; only without a BOM does it try UTF-8 → GBK → Latin-1. Write-back preserves the original BOM, encoding and newline style (CRLF stays CRLF). A file that had no BOM never gains one — this one bit us: early versions probed with `utf-8-sig`, which happily decodes plain UTF-8, so every edited file got stamped with a BOM and `JSON.parse` / shell scripts broke instantly.
- **No silent parameter surprises.** `edit` without `--new` stops with an error — it does **not** read "you said nothing" as "delete it" (it used to, while printing "edited"). To delete, write `--new ''` on purpose.
- **It always says what it cut.** Output over the limit is truncated, but a line always follows telling you how much was cut and how to see the rest. Don't skim past that `⚠ truncated` line — the answer may be in the part that got cut.
- **What didn't reach you is not "read".** A truncated read records **no line range**, so the next `read` hands you the body again instead of skipping it with "unchanged". That is this tool's one real promise.
- **Cache location is yours.** `FSTATE_HOME` moves it; `hot` shows what's accumulating and `reset` drops individual files.

---

## When NOT to use it

- **You already know the exact line numbers** → the native read tool is faster; caching is overhead.
- **One-shot work** (a single command, a script that runs once) → nothing happens twice, so a cache earns nothing.
- **Binary / images / files over 8 MB** → refused by design.

To be honest: **for a single-pass flow ("locate → read a chunk → edit one place → confirm"), this skill actually costs more** (measured 16.5% worse, because `explore` is pricier than a precise grep and `read` adds a line-number prefix).

It pays off when **the same content gets reused**: touching a file 4 times saves 65%, editing N places with one confirmation each saves 88%, understanding 20 files at once saves 80%.

---

## FAQ

**Q: Can it hand me stale content?**
No. Every read compares the current file's SHA-1 against the record first; a mismatch is treated as "changed outside" and a diff is computed from the shadow. The only degraded case is a missing shadow — and then it says outright that it is falling back to the full text.

**Q: Does it fight with the built-in read tool?**
No — it changes no native tool, it just gives you another option. The division of labour: **know the exact line numbers → native tool; about to touch the same file a second time → this skill.**

**Q: The cache is dirty / I want to start over.**

```bash
"$F" reset src/app.py         # one file
rm -rf ~/.dsh/fstate-cache    # everything (harmless, rebuilt on demand)
```

**Q: How much disk does it use?**
About 300 bytes of JSON per file plus a shadow copy (≤2 MB, only for files you actually read). A thousand-file project is usually a few MB to a few tens of MB. `hot` shows the biggest, `reset` drops them.

---

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `ERROR: 找不到 python` | the launcher couldn't find an interpreter | `export FSTATE_PYTHON=/path/to/python3` |
| `ERROR: 未找到 old 片段` | `--old` doesn't match the file (spaces / indentation) | print the lines with `read --mode range` and **copy them verbatim** into `--old`; newlines need no massaging |
| `old 片段出现 N 次，不唯一` | the fragment is too short and matches several places | add a line of context, or `--all` if you really mean all of them |
| `ERROR: 无法按文本读取（toolarge）` | file is over 8 MB | use the native tools, or split it first |
| `ERROR: 无法按文本读取（binary）` | contains NUL bytes | this skill does not touch binaries |
| edit succeeded but the next read says "changed" | your editor changed the file behind your back (formatter, save hook) | `state` shows SHA-1 and version — find out who wrote it |
| the skill isn't in the catalog | plugin didn't install, or DSH wasn't restarted | `dsh plugin --profile web list`; restart once |

> The skill's own messages are in Chinese, but the `ERROR:` prefix and exit codes (0 success / 2 failure) are stable, so scripts can match on those.

---

## Compatibility

Verified against DSH `0.2.1-rc.5`. The skill registers at rank `600` (bundled). If you also keep a local copy under `~/.dsh/dsh-skill/file-state-cache`, the local one wins.

## License

MIT
