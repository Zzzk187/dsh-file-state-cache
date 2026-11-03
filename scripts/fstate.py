#!/usr/bin/env python3
"""fstate.py — 文件状态缓存 CLI（省上下文用）。

核心思路：DSH 的单次读取工具没有记忆，同一个文件在两次 bash 之间被反复整篇读。
本脚本在磁盘上维护一份 per-file 状态（sha1 / 行数 / 上次读的范围 / 上次 diff），
让"刚读过且没变"能被识别出来，直接返回摘要而不是全文。

子命令：
  explore  QUERY            一次成型：目录概览 + 命中文件 + 命中行 + 签名 + 片段 + 下一步
  read     PATH             带缓存的读：未变→摘要；变了→diff；范围→行号区间
  edit     PATH --old/--new 编辑并回 unified diff + 变更行区间（不需要再读全文确认）
  state    PATH             版本 / 行数 / 读次数 / 上次编辑区间
  diff     PATH             取最近一次 diff
  reset    PATH             清缓存（外部改过文件、或怀疑状态不对时）
  hot      [--n N]          读得最多的文件排行（提醒自己别再重复读）

编码：输出强制 UTF-8；源文件按 utf-8-sig → gbk → latin-1 依次尝试。
长短输出：超过 --max-chars 会自动落盘到 spill 文件，只回预览 + 路径。
"""
from __future__ import annotations

import argparse
import difflib
import math
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 基础设施

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "dist", "build", "out", "obj", "bin",
    "__pycache__", ".venv", "venv", "env", ".next", ".nuxt", "target", "coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", ".cache",
    "vendor", "third_party", ".gradle", ".dart_tool", "Pods",
}
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pyc", ".class", ".jar", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".wav", ".db", ".sqlite", ".lock", ".node", ".asar",
}
DIGEST_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua",
    ".sh", ".ps1", ".sql", ".vue", ".svelte",
}
SKIP_NAME_RE = re.compile(
    r"^(?:pnpm-lock\.yaml|package-lock\.json|yarn\.lock|composer\.lock|"
    r"Cargo\.lock|poetry\.lock|Gemfile\.lock)$|.*\.min\.(?:js|css|mjs)$",
    re.I,
)
SIG_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:pub\s+)?"
    r"(?:def|class|function|interface|type|const|let|var|fn|struct|enum|impl|trait|func|module|namespace)\b"
    r"|^\s*(?:export\s+)?(?:async\s+)?function\s*\w+"
    r"|^\s*(?:public|private|protected|static|internal)\s+[\w<>\[\]]+\s+\w+\s*\(",
    re.M,
)

# 「定义」形态：比普通调用更能指向答案所在。
# 只数出现次数会让 tests/ 里调用 19 次的文件压过 src/ 里只出现 5 次的真正定义处。
DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+)?(?:async\s+)?"
    r"(?:def|class|function|interface|type|struct|enum|trait|fn|func)\s+{name}\b"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*[:=]"
    r"|^\s*@(?:app\.)?(?:route|get|post|put|delete)\b",
    re.M,
)

CACHE_HOME = Path(os.environ.get("FSTATE_HOME") or (Path.home() / ".dsh" / "fstate-cache"))
SHADOW_LIMIT = 2 * 1024 * 1024   # 只给 ≤2MB 的文件存影子副本，用于 diff
SKIP_SCAN_LIMIT = 2 * 1024 * 1024


def _key(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).lower().encode("utf-8")).hexdigest()[:20]


def def_boost(low: str, terms: list[str]) -> int:
    """这个词元在本文件里是「定义」形态吗？是则给一个大加分。

    动机（实测）：查 createWorkBuddyAdapter 时，tests/adapter.spec.ts 里调用 6 次、
    src/adapter.ts 里只在定义行出现 2 次 —— 只按出现次数排，答案被排到第 8 位，
    Agent 得先读几个测试文件才发现找错了。定义形态只出现在答案文件里。
    """
    got = 0
    for t in terms:
        if len(t) < 3:
            continue
        try:
            pat = DEF_RE.pattern.format(name=re.escape(t))
        except (KeyError, IndexError):
            continue
        if re.search(pat, low, re.M):
            got += 1
    return got * 18


def def_line(lines: list[str], terms: list[str]) -> int:
    """在文件里找定义所在行号。下一步建议要指向它，而不是第一个命中行
    ——命中行经常是注释或引用（`/** What {@link X} hands back. */`）。"""
    for t in terms:
        if len(t) < 3:
            continue
        try:
            pat = re.compile(DEF_RE.pattern.format(name=re.escape(t)), re.M | re.I)
        except (KeyError, IndexError):
            continue
        for i, ln in enumerate(lines, 1):
            if pat.match(ln):
                return i
    return 0


def _meta_path(path: Path) -> Path:
    return CACHE_HOME / f"{_key(path)}.json"


def _shadow_path(path: Path) -> Path:
    return CACHE_HOME / f"{_key(path)}.shadow"


def _load_meta(path: Path) -> dict:
    p = _meta_path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(path: Path, meta: dict) -> None:
    CACHE_HOME.mkdir(parents=True, exist_ok=True)
    _meta_path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def read_text(path: Path) -> tuple[str | None, str]:
    """返回 (文本, 编码)；二进制或不可读返回 (None, '')。"""
    if path.stat().st_size > 8 * 1024 * 1024:
        return None, "toolarge"
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        return None, "binary"
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, "unknown"


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_lines(text: str) -> list[str]:
    return text.splitlines()


def _emit(text: str, max_chars: int, kind: str) -> None:
    if len(text) <= max_chars:
        print(text)
        return
    spill_dir = CACHE_HOME / "spill"
    spill_dir.mkdir(parents=True, exist_ok=True)
    sp = spill_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.txt"
    sp.write_text(text, encoding="utf-8")
    head = text[: int(max_chars * 0.6)]
    tail = text[-int(max_chars * 0.3):]
    print(head)
    print(f"\n... [已截断 {len(text) - len(head) - len(tail)} 字符] ...\n")
    print(tail)
    print(f"\n[全文 {len(text)} 字符已落盘: {sp}]")


def unified(a: list[str], b: list[str], rel: str, ctx: int = 3) -> str:
    return "\n".join(
        difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=ctx, lineterm="")
    )


def hunks_new_range(diff_text: str) -> tuple[int, int] | None:
    """从 unified diff 的 @@ 头算出新文件侧的变更区间（1-based，含端点）。"""
    starts, ends = [], []
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_text, re.M):
        s = int(m.group(1))
        n = int(m.group(2) or 1)
        starts.append(s)
        ends.append(s + max(n, 1) - 1)
    if not starts:
        return None
    return min(starts), max(ends)


def changed_lines_range(a: list[str], b: list[str]) -> tuple[int, int] | None:
    """只算真正变动过的行（不含 diff 上下文），返回新文件侧 1-based 闭区间。"""
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    spans = [(j1 + 1, j2) for tag, _i1, _i2, j1, j2 in ops if tag != "equal" and j2 > j1]
    if not spans:
        # 纯删除：退化为删除位置附近一行
        dels = [(j1 + 1, j1 + 1) for tag, _i1, _i2, j1, j2 in ops if tag == "delete"]
        if not dels:
            return None
        spans = [(max(1, dels[0][0] - 1), max(1, dels[0][0]))]
    return min(s for s, _ in spans), max(e for _, e in spans)


def resolve_input(p: str) -> Path:
    """把用户给的路径变成真实文件；目录/不存在给出可操作的报错。"""
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path

# ---------------------------------------------------------------- read

def cmd_read(args: argparse.Namespace) -> int:
    path = resolve_input(args.path)
    if path.is_dir():
        print(f"ERROR: 这是目录不是文件: {path}")
        print(f"  想看目录内容请用: fstate.py explore <关键词> --root {path}")
        return 2
    if not path.is_file():
        print(f"ERROR: 文件不存在: {path}")
        return 2
    meta = _load_meta(path)
    disk = sha1_of(path)
    text, enc = read_text(path)
    if text is None:
        print(f"ERROR: 无法按文本读取（{enc}）: {path}")
        return 2
    lines = split_lines(text)
    total = len(lines)
    rel = args.label or path.name
    changed_vs_cache = bool(meta) and meta.get("sha1") != disk
    first_time = not meta

    mode = args.mode
    if mode == "auto":
        # 显式给了行范围就是想要局部正文，别拿「内容未变」把请求挡回去
        if args.start is not None or args.end is not None:
            mode = "range"
        elif not changed_vs_cache and meta.get("read_count", 0) > 0:
            mode = "cached"
        elif changed_vs_cache:
            mode = "changed"
        else:
            mode = "full"

    next_version = (meta.get("version") or 0)
    if first_time:
        next_version = 1
    elif changed_vs_cache:
        next_version += 1
    header = f"# {path}  版本 v{next_version}  {total} 行  [{enc}]"

    if mode == "cached":
        last = [f"{r[0]}..{r[1]}" for r in (meta.get("read_ranges") or [])][-3:]
        hint = (
            f"内容未变（sha1 相同）。此前已读 {meta.get('read_count')} 次；"
            f"最近读的范围: {last or ['全文']}。需要正文请用 --mode range/full。"
        )
        print(header)
        print(hint)
        if meta.get("last_edit_range"):
            print(f"上次编辑区间: {meta['last_edit_range'][0]}..{meta['last_edit_range'][1]}")
        _bump(path, meta, disk, lines, enc, mode="cached", rng=None, version=next_version)
        return 0

    if mode == "changed" and meta.get("sha1") != disk:
        shadow = _shadow_path(path)
        old_lines = []
        if shadow.is_file() and meta.get("shadow"):
            try:
                old_lines = split_lines(shadow.read_text(encoding="utf-8"))
            except Exception:
                old_lines = []
        if old_lines:
            d = unified(old_lines, lines, rel)
            rng = changed_lines_range(old_lines, lines) or hunks_new_range(d)
            print(header)
            print(f"## 磁盘内容已变（v{meta.get('version', 0)+1}），以下仅为差异：\n")
            _emit(d, args.max_chars, "diff")
            if rng:
                print(f"\n[变更行区间(新文件): {rng[0]}..{rng[1]}]  看局部: --mode range --start {rng[0]} --end {rng[1]}")
            _bump(path, meta, disk, lines, enc, mode="changed", rng=rng, diff=d, version=next_version)
            return 0
        # 没有影子副本 → 老实给全文
        mode = "full"

    if mode == "range":
        s = max(1, args.start or 1)
        e = min(total, args.end or min(total, s + 199))
        if s > total:
            print(f"ERROR: start={s} 超过总行数 {total}")
            return 2
        if e < s:
            print(f"ERROR: 区间反向（start={s} > end={e}）。文件共 {total} 行，"
                  f"想看末尾用: --mode range --start {max(1, total - 20)} --end {total}")
            return 2
        body = "\n".join(f"{i:5d}| {lines[i-1]}" for i in range(s, e + 1))
        print(f"{header}\n## 行 {s}..{e} / 共 {total} 行\n{body}")
        _bump(path, meta, disk, lines, enc, mode="range", rng=(s, e), version=next_version)
        return 0

    # full：受 --max-lines 约束，超了显式告知如何分页
    limit = args.max_lines or total
    show = lines[:limit]
    body = "\n".join(f"{i:5d}| {ln}" for i, ln in enumerate(show, 1))
    out = f"{header}\n{body}"
    if limit < total:
        out += (
            f"\n\n[已显示前 {limit}/{total} 行。继续: --mode range --start {limit+1} --end {min(total, limit+300)}]"
        )
    _emit(out, args.max_chars, "read")
    _bump(path, meta, disk, lines, enc, mode="full", rng=(1, limit), version=next_version)
    return 0


def _bump(path: Path, meta: dict, disk: str, lines: list[str], enc: str,
          *, mode: str, rng, diff: str | None = None, version: int | None = None) -> None:
    now = time.time()
    ranges = meta.get("read_ranges") or []
    if rng:
        ranges.append(list(rng))
    if version is None:
        version = (meta.get("version") or 0)
        if not meta.get("sha1"):
            version = 1
        elif meta.get("sha1") != disk:
            version += 1
    # 缓存命中不改 sha1，但首次写缓存时必须落下 sha1
    meta.update({
        "path": str(path.resolve()),
        "sha1": disk,
        "lines": len(lines),
        "encoding": enc,
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
        "version": version,
        "read_count": meta.get("read_count", 0) + 1,
        "last_read_at": now,
        "read_ranges": ranges[-20:],
    })
    if path.stat().st_size <= SHADOW_LIMIT and mode in ("full", "changed", "cached"):
        if not meta.get("shadow") or mode in ("full", "changed"):
            try:
                # 缓存目录在首次使用时还不存在；不建目录就直接写会抛异常，
                # 而下面的 except 会把它吞成 shadow=False，导致下次外部修改拿不到 diff。
                CACHE_HOME.mkdir(parents=True, exist_ok=True)
                _shadow_path(path).write_text("\n".join(lines), encoding="utf-8")
                meta["shadow"] = True
            except Exception as exc:  # 真失败了要知道，别静默降级
                meta["shadow"] = False
                meta["shadow_error"] = f"{type(exc).__name__}: {exc}"
    if diff:
        meta["last_diff"] = diff[:200_000]
    _save_meta(path, meta)


# ---------------------------------------------------------------- edit

def cmd_edit(args: argparse.Namespace) -> int:
    path = resolve_input(args.path)
    if path.is_dir():
        print(f"ERROR: 这是目录不是文件: {path}")
        return 2
    if not path.is_file():
        print(f"ERROR: 文件不存在: {path}")
        return 2
    text, enc = read_text(path)
    if text is None:
        print(f"ERROR: 无法按文本编辑（{enc}）")
        return 2
    old = Path(args.old_file).read_text(encoding="utf-8") if args.old_file else args.old
    new = Path(args.new_file).read_text(encoding="utf-8") if args.new_file else (args.new or "")
    if not old:
        print("ERROR: 需要 --old/--old-file")
        return 2

    # 换行宽容：调用方传片段时几乎总是 LF，而 Windows 上的文件多为 CRLF。
    # 不归一的话同一段文本也会匹配失败，逼调用方手动转换行符。
    if "\r\n" in text and "\r\n" not in old:
        old = old.replace("\r\n", "\n").replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    elif "\r\n" not in text and "\r\n" in old:
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")

    count = text.count(old)
    if count == 0:
        print("ERROR: 未找到 old 片段。相近行候选（用行号微调后再试）：")
        needle = old.strip().splitlines()[0] if old.strip() else ""
        best = difflib.get_close_matches(needle, split_lines(text), n=3, cutoff=0.4)
        for b in best:
            for i, ln in enumerate(split_lines(text), 1):
                if ln == b:
                    print(f"  {i:5d}| {ln[:160]}")
                    break
        return 3
    if count > 1 and not args.all:
        print(f"ERROR: old 片段出现 {count} 次，不唯一。加 --all 或扩大上下文使其唯一。")
        return 3

    old_lines = split_lines(text)
    new_text = text.replace(old, new) if args.all else text.replace(old, new, 1)
    new_lines = split_lines(new_text)
    path.write_text(new_text, encoding=enc if enc != "utf-8-sig" else "utf-8-sig", newline="")
    diff = unified(old_lines, new_lines, args.label or path.name)
    rng = changed_lines_range(old_lines, new_lines) or hunks_new_range(diff)

    meta = _load_meta(path)
    disk = sha1_of(path)
    meta.update({
        "path": str(path.resolve()), "sha1": disk, "lines": len(new_lines),
        "encoding": enc, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
        "version": meta.get("version", 0) + 1,
        "last_edit_at": time.time(), "last_edit_range": list(rng) if rng else None,
        "last_diff": diff[:200_000], "reason": args.reason or "",
    })
    if path.stat().st_size <= SHADOW_LIMIT:
        try:
            _shadow_path(path).write_text("\n".join(new_lines), encoding="utf-8")
            meta["shadow"] = True
        except Exception:
            meta["shadow"] = False
    _save_meta(path, meta)

    print(f"# 已改 {path}  → v{meta['version']}  {len(new_lines)} 行"
          + (f"  区间 {rng[0]}..{rng[1]}" if rng else ""))
    if args.reason:
        print(f"# reason: {args.reason}")
    print(diff)
    if rng:
        print(f"\n[确认改动不必重读全文：看局部用 --mode range --start {rng[0]} --end {rng[1]}]")
    return 0


# ---------------------------------------------------------------- write

def cmd_write(args: argparse.Namespace) -> int:
    """新建/整体覆盖：老内容存在就给 diff，不存在就只报行数与前几行。"""
    path = resolve_input(args.path)
    if path.is_dir():
        print(f"ERROR: 这是目录不是文件: {path}")
        return 2
    if args.content_file:
        body = Path(args.content_file).read_text(encoding="utf-8")
    elif args.stdin:
        body = sys.stdin.read()
    else:
        print("ERROR: 需要 --content-file 或 --stdin")
        return 2
    if not body.endswith("\n"):
        body += "\n"

    enc = args.encoding or "utf-8"
    old_lines: list[str] = []
    existed = path.is_file()
    if existed:
        old_text, old_enc = read_text(path)
        if old_text is not None:
            old_lines = split_lines(old_text)
            if not args.encoding:
                enc = "utf-8-sig" if old_enc == "utf-8-sig" else old_enc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding=enc, newline="")

    new_lines = split_lines(body)
    meta = _load_meta(path)
    diff = unified(old_lines, new_lines, args.label or path.name) if old_lines else ""
    rng = (changed_lines_range(old_lines, new_lines) or hunks_new_range(diff)) if old_lines else (1, len(new_lines))
    meta.update({
        "path": str(path.resolve()), "sha1": sha1_of(path), "lines": len(new_lines),
        "encoding": enc, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
        "version": (meta.get("version") or 0) + 1,
        "last_edit_at": time.time(), "last_edit_range": list(rng) if rng else None,
        "last_diff": diff[:200_000], "reason": args.reason or "",
    })
    if path.stat().st_size <= SHADOW_LIMIT:
        try:
            _shadow_path(path).write_text("\n".join(new_lines), encoding="utf-8")
            meta["shadow"] = True
        except Exception:
            meta["shadow"] = False
    _save_meta(path, meta)

    head = "已改" if existed else "已新建"
    print(f"# {head} {path}  → v{meta['version']}  {len(new_lines)} 行  [{enc}]")
    if args.reason:
        print(f"# reason: {args.reason}")
    if diff:
        _emit(diff, args.max_chars, "diff")
    else:
        preview = "\n".join(f"{i:5d}| {ln}" for i, ln in enumerate(new_lines[:10], 1))
        print(preview)
        if len(new_lines) > 10:
            print(f"... 共 {len(new_lines)} 行")
    return 0


# ---------------------------------------------------------------- digest

def cmd_digest(args: argparse.Namespace) -> int:
    """一次看多个文件的结构（行数 + 顶层签名 + 缓存状态），替代逐个 read。"""
    targets: list[Path] = []
    skipped: list[Path] = []
    all_ext = bool(getattr(args, "all_ext", False))
    for raw in args.paths:
        p = resolve_input(raw)
        if p.is_dir():
            for f in iter_files(p, args.max_scan):
                if all_ext or f.suffix.lower() in DIGEST_EXT or not DIGEST_EXT:
                    targets.append(f)
                else:
                    skipped.append(f)
        elif p.is_file():
            targets.append(p)
    seen = set()
    uniq = []
    for t in targets:
        k = str(t.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    uniq = uniq[: args.max_files]
    if not uniq:
        print("ERROR: 没有可摘要的文件（目录里没有常见源码后缀？可用 --max-files 放宽）")
        return 2

    print(f"# 结构摘要  {len(uniq)} 个文件")
    if skipped:
        names = "、".join(x.name for x in skipped[:6])
        more = f" 等 {len(skipped)} 个" if len(skipped) > 6 else ""
        print(f"# 跳过 {len(skipped)} 个非源码后缀文件（{names}{more}）—— 要一起看就加 --all-ext")
        print("#   （默认只摘要源码后缀；被跳过不等于目录里没有）")
    for p in uniq:
        text, enc = read_text(p)
        meta = _load_meta(p)
        if p.is_dir():
            continue
        try:
            rel = str(p.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
        except Exception:
            rel = p.name
        if text is None:
            print(f"\n## {rel}  [跳过: {enc}]")
            continue
        lines = split_lines(text)
        cached = ""
        if meta:
            same = meta.get("sha1") == sha1_of(p)
            cached = f"  已读 {meta.get('read_count', 0)} 次" + ("" if same else "  ⚠磁盘已变")
        print(f"\n## {rel}  {len(lines)} 行  [{enc}]{cached}")
        if len(lines) < 300 and max((len(l) for l in lines), default=0) > 5000:
            print("  （打包产物，跳过签名）")
            continue
        sigs = []
        truncated = False
        for i, ln in enumerate(lines, 1):
            if SIG_RE.match(ln):
                if len(sigs) >= args.max_sigs:
                    truncated = True
                    break
                sigs.append(f"{i:5d}| {ln.strip()[:140]}")
        if sigs:
            print("\n".join("  " + x for x in sigs))
            if truncated:
                print(f"  ... 签名未列完（--max-sigs {args.max_sigs}），看局部用 read --mode range")
        else:
            head = "\n".join(
                f"{i:5d}| {lines[i-1][:120]}" for i in range(1, min(len(lines), 5) + 1)
            )
            print("  " + head.replace("\n", "\n  "))
    return 0


# ---------------------------------------------------------------- state / diff / reset / hot

def cmd_state(args: argparse.Namespace) -> int:
    path = resolve_input(args.path)
    meta = _load_meta(path)
    if not meta:
        print(f"{path}: 无缓存（还没用 fstate read 过）")
        return 0
    disk = sha1_of(path) if path.is_file() else "missing"
    stale = "STALE（磁盘已变，需要 --mode full 重新加载）" if disk != meta.get("sha1") else "fresh"
    print(json.dumps({
        "path": meta.get("path"),
        "version": meta.get("version"),
        "lines": meta.get("lines"),
        "read_count": meta.get("read_count"),
        "last_edit_range": meta.get("last_edit_range"),
        "last_read_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("last_read_at", 0)))
        if meta.get("last_read_at") else None,
        "cache_state": stale,
    }, ensure_ascii=False, indent=1))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    meta = _load_meta(resolve_input(args.path))
    d = meta.get("last_diff")
    if not d:
        print("没有记录到 diff（还没用本脚本编辑过该文件）")
        return 0
    _emit(d, args.max_chars, "diff")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    path = resolve_input(args.path)
    for p in (_meta_path(path), _shadow_path(path)):
        if p.is_file():
            p.unlink()
    print(f"已清缓存: {path}")
    return 0


def cmd_hot(args: argparse.Namespace) -> int:
    rows = []
    if CACHE_HOME.is_dir():
        for f in CACHE_HOME.glob("*.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append((m.get("read_count", 0), m.get("path", "?"), m.get("lines", 0)))
    rows.sort(reverse=True)
    print("读次数  行数   文件")
    for c, p, n in rows[: args.n]:
        flag = "  ← 反复读，考虑改用 range/diff" if c >= 5 else ""
        print(f"{c:6d}  {n:5d}   {p}{flag}")
    return 0


# ---------------------------------------------------------------- explore

def iter_files(root: Path, max_files_scan: int):
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in SKIP_EXT or fn.startswith(".") or SKIP_NAME_RE.match(fn):
                continue
            p = Path(dirpath) / fn
            try:
                if p.stat().st_size > SKIP_SCAN_LIMIT:
                    continue
            except OSError:
                continue
            seen += 1
            if seen > max_files_scan:
                return
            yield p


def cmd_explore(args: argparse.Namespace) -> int:
    root = Path(args.root or ".").resolve()
    if not root.is_dir():
        print(f"ERROR: 目录不存在: {root}")
        return 2
    q = args.query.strip()
    if not q:
        print("ERROR: 关键词不能为空（空关键词会匹配所有文件）。")
        print("  给个词再试，例如: fstate.py explore \"TODO\" --root .")
        return 2
    terms = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", q.lower()) if len(t) >= 2] or [q.lower()]
    terms = [t for t in terms if t]
    if not terms:
        print(f"ERROR: 关键词 {q!r} 拆不出有效词元（每个词元至少 2 个字符）。")
        return 2
    phrase = q.lower()

    hits = []
    ext_count: dict[str, int] = {}
    dir_count: dict[str, int] = {}
    scanned = 0
    skipped_bundle = 0
    for p in iter_files(root, args.max_scan):
        scanned += 1
        rel = str(p.relative_to(root)).replace("\\", "/")
        ext_count[p.suffix.lower() or "(none)"] = ext_count.get(p.suffix.lower() or "(none)", 0) + 1
        top = rel.split("/")[0] if "/" in rel else "."
        dir_count[top] = dir_count.get(top, 0) + 1

        low_rel = rel.lower()
        score = 0.0
        for t in terms:
            if t in p.name.lower():
                score += 10
            if t in low_rel:
                score += 4
        text, _ = read_text(p)
        if text is None:
            if score:
                hits.append({"rel": rel, "abs": str(p), "score": score, "lines": [], "sigs": [], "snip": ""})
            continue
        lines = split_lines(text)
        # 打包/压缩产物：存在超长行（bundle、内联脚本）。读它的片段没意义，跳过
        if len(lines) < 300 and max((len(l) for l in lines), default=0) > 5000:
            skipped_bundle += 1
            continue
        low = text.lower()
        occ = 0
        for t in terms:
            c = low.count(t)
            occ += c
            score += min(c, 12) * 1.0
        if phrase in low and len(phrase) >= 4:
            score += 8
        # 只按内容出现计分，文件名命中不足以入选（否则 index.html 这类会压过真答案）
        if occ == 0:
            continue
        # 定义优先：答案通常在定义处，而调用处往往更多
        score += def_boost(low, terms)
        if score < args.min_score:
            continue
        # 命中行
        matched = []
        for i, ln in enumerate(lines, 1):
            if any(t in ln.lower() for t in terms):
                matched.append((i, ln.strip()[:200]))
                if len(matched) >= args.max_hits:
                    break
        # 签名：优先取命中行附近的，否则取文件前部
        sigs: list[str] = []
        near = {i for i, _ in matched}
        for i, ln in enumerate(lines, 1):
            if SIG_RE.match(ln):
                if near and not any(abs(i - n) <= 40 for n in near):
                    continue
                sigs.append(f"{i:5d}| {ln.strip()[:140]}")
                if len(sigs) >= args.max_sigs:
                    break
        # 片段只给「命中行之外」的上下文；命中行上面已经列过了，重复一遍纯浪费
        mset = {i for i, _ in matched}
        first = matched[0][0] if matched else 1
        ctx: list[str] = []
        for i in range(first, min(len(lines), first + args.snippet_lines * 3) + 1):
            if i in mset:
                continue
            ctx.append(f"{i:5d}| {lines[i-1][:160]}")
            if len(ctx) >= args.snippet_lines:
                break
        snip = "\n".join(ctx)
        # 长度惩罚：同样几次命中，短文件比长文件更有信息量（避免大文件靠体量霸榜）
        score = score / (1.0 + math.log10(max(1, len(lines)) / 60.0) if len(lines) > 60 else 1.0)
        hits.append({"rel": rel, "abs": str(p), "score": round(score, 1),
                     "isdef": bool(def_boost(low, terms)),
                     "defline": def_line(lines, terms),
                     "lines": [f"{i:5d}| {t}" for i, t in matched], "sigs": sigs, "snip": snip})

    hits.sort(key=lambda h: h["score"], reverse=True)
    top = hits[: args.max_files]
    # 「唯一强定义」：全库只有一个文件带定义命中。此时答案已经没悬念，
    # 概览/签名/完整其余列表都是噪音，压掉能省一半输出。
    strong = bool(top) and bool(top[0].get("isdef")) and (
        len(top) == 1 or top[0]["score"] >= top[1]["score"] * 1.25
    )

    print(f"# 探索  query={q!r}  root={root}")
    print(f"扫描 {scanned} 个文件 -> 命中 {len(hits)} 个，显示 {len(top)} 个"
          + (f"（其余 {len(hits)-len(top)} 个看 --max-files）" if len(hits) > len(top) else ""))
    # 概览压成两行：目录/后缀只在「一眼看结构」时有用，不值得占几十行。
    # 已有唯一强定义时（答案没悬念）这两行纯属噪音。
    if not strong:
        dirs = " | ".join(f"{d} {c}" for d, c in sorted(dir_count.items(), key=lambda x: -x[1])[:6])
        exts = " ".join(f"{e}:{c}" for e, c in sorted(ext_count.items(), key=lambda x: -x[1])[:8])
        print(f"  目录: {dirs}")
        print(f"  后缀: {exts}")
    print()
    for idx, h in enumerate(top):
        mark = "  \u2605\u5b9a\u4e49" if h.get("isdef") else ""
        dl = f":{h['defline']}" if h.get("defline") else ""
        print(f"### {h['rel']}{dl}  score={h['score']}{mark}")
        if h["lines"]:
            for ln in h["lines"]:
                print("  命中 " + ln.strip())
        # 签名与片段只给第一个文件：第一名是答案的概率最高，其余给了也是噪音
        if h["sigs"] and not strong and (idx == 0 or len(top) <= 2):
            for s in h["sigs"][:3]:
                print("  签名 " + s.strip())
        if h["snip"] and idx == 0:
            for ln in h["snip"].splitlines()[: args.snippet_lines]:
                print("  片段 " + ln.strip())
    # 被截断的文件只列路径：一行几十字节，换来「没有东西被藏起来」
    rest = hits[len(top):]
    if rest:
        cap = 5 if strong else 10
        names = ", ".join(h["rel"] for h in rest[:cap])
        more = f" 等 {len(rest)} 个" if len(rest) > cap else ""
        print()
        print(f"其余: {names}{more}  （要详情加 --max-files）")
    print()
    if top:
        h0 = top[0]
        first = h0.get("defline") or 0
        if not first:
            try:
                first = int(h0["lines"][0].split("|")[0]) if h0["lines"] else 1
            except Exception:
                first = 1
        print(f"下一步: fstate.py read \"{h0['rel']}\" --mode range --start {max(1, first-5)} --end {first+40}")
        print(f"        fstate.py edit \"{h0['rel']}\" --old ... --new ...   （回 diff，不必重读全文）")
    else:
        print("无命中。换个说法（更短的词）、或加大 --max-scan / 换 --root。")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(prog="fstate.py", description="文件状态缓存 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("explore", help="一次成型探索")
    e.add_argument("query")
    e.add_argument("--root", default=".")
    e.add_argument("--max-files", type=int, default=3)
    e.add_argument("--max-hits", type=int, default=3)
    e.add_argument("--max-sigs", type=int, default=6)
    e.add_argument("--snippet-lines", type=int, default=4)
    e.add_argument("--max-scan", type=int, default=6000)
    e.add_argument("--min-score", type=float, default=3.0)
    e.add_argument("--max-chars", type=int, default=24000)
    e.set_defaults(fn=cmd_explore)

    r = sub.add_parser("read", help="带缓存的读")
    r.add_argument("path")
    r.add_argument("--mode", default="auto", choices=["auto", "full", "range", "diff", "changed", "cached"])
    r.add_argument("--start", type=int)
    r.add_argument("--end", type=int)
    r.add_argument("--max-lines", type=int, default=400)
    r.add_argument("--max-chars", type=int, default=20000)
    r.add_argument("--label", default="")
    r.set_defaults(fn=cmd_read)

    w = sub.add_parser("edit", help="编辑并回 diff")
    w.add_argument("path")
    w.add_argument("--old", default="")
    w.add_argument("--new", default="")
    w.add_argument("--old-file", default="")
    w.add_argument("--new-file", default="")
    w.add_argument("--all", action="store_true")
    w.add_argument("--reason", default="")
    w.add_argument("--label", default="")
    w.set_defaults(fn=cmd_edit)

    wr = sub.add_parser("write", help="新建/覆盖并回 diff")
    wr.add_argument("path")
    wr.add_argument("--content-file", default="")
    wr.add_argument("--stdin", action="store_true")
    wr.add_argument("--encoding", default="")
    wr.add_argument("--reason", default="")
    wr.add_argument("--label", default="")
    wr.add_argument("--max-chars", type=int, default=12000)
    wr.set_defaults(fn=cmd_write)

    s = sub.add_parser("state", help="文件状态")
    s.add_argument("path")
    s.set_defaults(fn=cmd_state)

    dg = sub.add_parser("digest", help="多文件结构摘要")
    dg.add_argument("paths", nargs="+")
    dg.add_argument("--max-files", type=int, default=20)
    dg.add_argument("--max-sigs", type=int, default=12)
    dg.add_argument("--max-scan", type=int, default=400)
    dg.add_argument("--all-ext", action="store_true", help="连非源码后缀（.md/.txt/无后缀）也摘要")
    dg.set_defaults(fn=cmd_digest)

    d = sub.add_parser("diff", help="最近一次 diff")
    d.add_argument("path")
    d.add_argument("--max-chars", type=int, default=12000)
    d.set_defaults(fn=cmd_diff)

    rs = sub.add_parser("reset", help="清缓存")
    rs.add_argument("path")
    rs.set_defaults(fn=cmd_reset)

    h = sub.add_parser("hot", help="读得最多的文件")
    h.add_argument("--n", type=int, default=15)
    h.set_defaults(fn=cmd_hot)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
