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

- **Runtime: Windows 10 / 11 only.** This project targets Windows exclusively; Linux / macOS / cross-platform support is out of scope.
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

> **Need a whole large file?** `read` is capped by auto's 400 lines *and* `--max-chars 20000`; when it cuts, it says so ("not counted as read"). To actually hand the full body to the Agent, use `read FILE --mode full --max-chars <big enough>` — don't let the default truncation masquerade as a token saving.

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
> - The summary only holds **within the same `DSH_SESSION_ID`**. A changed session ID gets the body again; but if the host compacts the context **without** changing that environment variable, the receipt does **not** auto-expire (the tool cannot observe a context compaction). When unsure, run `reset`. That is deliberate: saving context must not turn into "making you think you already saw it".

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
- `--brief` on `explore` / `digest` (since 1.1.1): keep only file + hit lines + the next command; drops the directory/extension overview, signatures, snippets, the edit suggestion and repeated per-file "signatures truncated" notes. **Locators and every ⚠ warning are kept**, what was cut is stated, and without the switch the output is byte-for-byte identical

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

> **A deliberate limit**: the "I already delivered this body" receipt is valid **only within the same `DSH_SESSION_ID`**, and is issued **only when a full body was actually delivered** — partial reads, truncated reads, and diff views never earn it. A changed session ID makes `read` hand you the body again instead of dismissing you with "unchanged". **Caveat**: if the host compacts context but keeps the same env var, the tool cannot tell and the receipt stays valid — `reset` is the escape hatch. A tool that saves context must never manufacture the illusion of having seen something.

---

## Design decisions worth knowing

- **Encoding is never changed behind your back.** A real BOM (UTF-8/16/32, either endianness) decides the encoding; only without a BOM does it try UTF-8 → GBK → Latin-1. Write-back preserves the original BOM, encoding and newline style (CRLF stays CRLF). A file that had no BOM never gains one — this one bit us: early versions probed with `utf-8-sig`, which happily decodes plain UTF-8, so every edited file got stamped with a BOM and `JSON.parse` / shell scripts broke instantly.
- **No silent parameter surprises.** `edit` without `--new` stops with an error — it does **not** read "you said nothing" as "delete it" (it used to, while printing "edited"). To delete, write `--new ''` on purpose.
- **It always says what it cut.** Output over the limit is truncated, but a line always follows telling you how much was cut and how to see the rest. Don't skim past that `⚠ truncated` line — the answer may be in the part that got cut.
- **What didn't reach you is not "read".** A truncated read records **no line range**, so the next `read` hands you the body again instead of skipping it with "unchanged". That is this tool's one real promise.
- **Cache location is yours.** `FSTATE_HOME` moves it; `hot` shows what's accumulating and `reset` drops individual files.

---

## When to use it / when NOT to

1.1.1 ships a reproducible benchmark (`perf-eval/`): A = native Windows reads, B = this tool, small / medium / large files, warmup + 5 repeats, median, real BPE token counts. One-line verdict: **it saves context (tokens), not time** — a cached hit still starts a fresh process, B ≈ 100~110ms per call vs A ≈ 55~62ms. So there is only one question: *will this happen a second time?*

**Worth it (the same content is reused):**

| Case | Measured token saving |
|---|---|
| Same session, re-reading a medium/large file 5 times | ~75~79% |
| Same session, re-reading a medium/large file 10 times | ~84~89% |
| Large/medium `edit` verified by diff (no full re-read) | ~46~49% |
| External change where a diff alone is acceptable | ~46~49% |
| `explore` then repeated `read` in one session (≥5) | ~60~72% |

**Not worth it (no saving, or worse):**

- **First read**: ~0% token difference, 81~100% slower.
- **Cross-session / changed `DSH_SESSION_ID`**: receipt void, body re-delivered, ~0% token difference and 95~113% slower.
- **Forcing the full post-change body**: ~0% token difference.
- **Small files**: even 10 reads save only ~28%, `edit` ~6%, and a one-shot `explore`+`read` costs ~68% *more* tokens.
- **Large/medium `digest`**: default prints several signatures + repeated notes per file — ~109~114% *more* tokens than native (a small directory saves ~18%); `--brief` narrows this to −3~−15%.
- **Exact line numbers known / one-shot scripts / binaries / >8 MB**: caching is overhead, or refused by design.

> These numbers use the `cl100k_base` real BPE encoder and are **not the target model's billing vocabulary**; the target model's absolute billed tokens and a real end-to-end Agent run (with inference) were not measured. See `perf-eval/REPORT.md`.

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
| `ERROR: 无法按文本读取（binary）` | contains NUL bytes, or too many control characters | this skill does not touch binaries (incl. non-text byte streams other than valid UTF-16/32 BOM files) |
| edit succeeded but the next read says "changed" | your editor changed the file behind your back (formatter, save hook) | `state` shows SHA-1 and version — find out who wrote it |
| the skill isn't in the catalog | plugin didn't install, or DSH wasn't restarted | `dsh plugin --profile web list`; restart once |

> The skill's own messages are in Chinese, but the `ERROR:` prefix and exit codes are stable, so scripts can match on them: **0** success, **2** failure, **3** `edit` fragment not found / not unique, **130** interrupted (Ctrl-C).

---

## Regression tests (reproducible)

Since 1.1.0 the tests ship with the repo and can be re-run — no more one-off scripts that readers cannot verify. 1.1.1 adds `tests/test_brief_output.py` (11 cases) covering `--brief` default-output identity, retained locators and every ⚠, no read receipt created, and no redundant note on small directories.

```bash
python -m pytest tests/ -q
```

Requires Python 3.8+ and pytest (**test-time only; the runtime still has zero third-party dependencies**). 94 cases cover cache hits / external edits / delete+recreate / truncated reads / cross-session receipts / shadow loss & tampering, BOM·GBK·UTF-16·CRLF·newline·empty-file fidelity, binary & 8 MB rejection, hardlinks/symlinks, multi-process concurrent edits without lost updates, corrupted-JSON self-healing, unwritable cache dir, spill filename collisions, `--min-score` finiteness, illegal-stdin-byte rejection, observable scan pruning, `--brief` default-compat & receipt guard, plus **Windows-specific** real exclusive-handle locking / read-only attribute / `os.replace` failure / crash-while-holding-lock leaves no deadlock / concurrent multi-process IO / corrupted cache JSON / shadow loss & corruption. Verified on **Windows 10 Pro (NT 10.0.26300)** with both **Python 3.13.13 / pytest 9.2.0** and **Python 3.8.20 / pytest 8.3.4** via `python -m pytest tests/ -q`: **94 passed** on both. This project targets Windows 10/11 only; **Linux / macOS / cross-platform support and verification are out of scope** (a scope boundary, not an unverified item).

---

## Compatibility

Verified against DSH `0.2.1-rc.5`. The skill registers at rank `600` (bundled). If you also keep a local copy under `~/.dsh/dsh-skill/file-state-cache`, the local one wins.

## License

MIT
