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

编码：输出强制 UTF-8；源文件先认 BOM，再按 utf-8 → gbk → latin-1 依次尝试，
      写回保持原 BOM / 原编码 / 原换行风格（BOM 只认真的 BOM，不拿 utf-8-sig 去试解码）。
长短输出：超过 --max-chars 会自动落盘到 spill 文件，只回预览 + 路径。
"""
from __future__ import annotations

import argparse
import codecs
import difflib
import math
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- 基础设施

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _positive_line(raw: str) -> int:
    """argparse 的 type 校验：行号必须 ≥1（1-based）。

    以前 `--start 0` 会被 `max(1, args.start)` 悄悄抬到 1，于是「--start 0 --end 2」
    静默回报第 1..2 行 —— 调用方以为自己在读第 0 行往后，实际偏移一行且毫无提示。
    这类「纠正得很合理、但没告诉你纠正了」的宽容，和静默截断是同一类毛病。
    """
    try:
        val = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"行号得是整数，收到 {raw!r}") from None
    if val < 1:
        raise argparse.ArgumentTypeError(f"行号从 1 开始，收到 {val}（0 和负数无意义）")
    return val


def _read_stdin_text() -> str:
    """读 stdin 的全部文本。

    Windows 上管道/重定向进来的是**系统 ANSI 页**的字节（本机 cp936），不是 UTF-8。
    `sys.stdin` 的默认解码器带 surrogateescape 时，中文会变成孤立代理字符：读的时候
    不报错，等 `body.encode("utf-8")` 才炸，错误位置离现场很远。所以这里绕过文本层，
    按字节读，显式按 utf-8-sig -> gbk 试。
    """
    raw = sys.stdin.buffer.read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_piece(file_arg: str, inline: str, flag: str) -> tuple[str, str]:
    """读 --old-file / --new-file 这类「片段来自文件」的参数。

    返回 (内容, 错误信息)。出错时把 OSError 变成一句人话 —— 之前这里直接
    让 FileNotFoundError 冒到顶，用户看到的是一屏 Traceback。
    """
    if not file_arg:
        return inline, ""
    p = Path(file_arg)
    if not p.is_file():
        return "", f"ERROR: {flag} 指向的文件不存在: {file_arg}"
    try:
        # 内容片段可能带 BOM（Windows 编辑器很爱加），utf-8-sig 会顺手剥掉
        return p.read_text(encoding="utf-8-sig"), ""
    except OSError as exc:
        return "", f"ERROR: 读不了 {flag}（{file_arg}）：{exc.strerror or exc}"
    except UnicodeDecodeError:
        return "", f"ERROR: {flag} 不是 UTF-8/GBK 文本: {file_arg}"


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


#: 认得出来的 BOM 及其对应解码器。**顺序有讲究**：UTF-32-LE 的 BOM 前两字节
#: 与 UTF-16-LE 完全相同，长的必须排在前面先试。
BOM_TABLE: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

#: 无 BOM 时的回退顺序。utf-8 严格解码在第一位：它失败才是「真不是 UTF-8」，
#: 能成功就说明没有 BOM 也该按 utf-8 写字。
FALLBACK_ENCODINGS = ("utf-8", "gbk", "latin-1")


def _detect_newline(text: str) -> tuple[str, bool]:
    """返回 (主换行风格, 是否混用)。

    为什么要探测：写回时用 `newline=""` 拼回原文，换行风格完全由 text 自己决定。
    只要 text 保持原样（我们只做字符串替换，不重排换行），编辑一处就只改动那一处，
    不会把整篇 CRLF 冲成 LF —— 1.0.5 之前 write 覆盖整篇时正是栽在这里。
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    counts = [(crlf, "\r\n"), (lf, "\n"), (cr, "\r")]
    top = max(counts, key=lambda x: (x[0], -len(x[1])))  # 同分时优先最短风格，避免把 LF 判成 CRLF
    mixed = sum(1 for c, _ in counts if c > 0) > 1
    return (top[1] if top[0] else ""), mixed


@dataclass(frozen=True)
class TextFile:
    """一个已解码的文本文件，连同「怎么写回去才跟原来一样」的全部信息。

    存在的理由：1.0.4 之前只记了 `enc` 一个字符串，于是三件事一起出错 ——
      · 探测用 `utf-8-sig` 打头，它对**没有** BOM 的 UTF-8 也解码成功，
        导致每个文件的 enc 都是 "utf-8-sig"，写回时无差别插一个 EF BB BF；
      · 换行风格没有记录，`write` 整篇覆盖把 CRLF 冲成 LF；
      · BOM 与编码混在一个字段里，没法表达「GBK 且无 BOM」这种组合。
    现在三者分开存，写回一律走 `encode()`，字节层面与原文件对齐。
    """

    text: str        # 全文，BOM 已剥、换行原样保留
    encoding: str    # 用于 str.encode 的 codec 名（不含 BOM 语义）
    bom: bytes       # 文件开头真实的 BOM 字节；无 BOM 为 b""

    @property
    def newline(self) -> str:
        return _detect_newline(self.text)[0]

    @property
    def mixed_newline(self) -> bool:
        return _detect_newline(self.text)[1]

    @property
    def label(self) -> str:
        """给人看的短标签，出现在 read/digest/write 的表头里。"""
        parts = [self.encoding]
        if self.bom:
            parts.append("BOM")
        nl, mixed = _detect_newline(self.text)
        if nl == "\r\n":
            parts.append("CRLF混用" if mixed else "CRLF")
        elif nl == "\r":
            parts.append("CR")
        elif mixed:
            parts.append("混用换行")
        return " ".join(parts)

    def encode(self, text: str) -> bytes:
        """把文本按「原编码 + 原 BOM」编码成字节。"""
        return self.bom + text.encode(self.encoding)


def load(path: Path) -> tuple[TextFile | None, str]:
    """读文件并判定编码/换行；不可读返回 (None, 原因)。

    探测顺序：**先认真的 BOM**，没有 BOM 才谈回退。这是 1.0.5 的核心修复 ——
    用 utf-8-sig 当探测器会把「无 BOM 的 UTF-8」谎报成「有 BOM」，写回就多出三个字节。
    """
    if path.stat().st_size > 8 * 1024 * 1024:
        return None, "toolarge"
    raw = path.read_bytes()

    bom, codec = b"", ""
    for marker, name in BOM_TABLE:
        if raw.startswith(marker):
            bom, codec = marker, name
            break

    if not bom:
        # 只有「无 BOM」的文件才用 NUL 判二进制；UTF-16/32 正文里满是 NUL，
        # 但它们已经被上面认领走了。
        if b"\x00" in raw[:8192]:
            return None, "binary"

    body = raw[len(bom):]
    if codec:
        try:
            text = body.decode(codec)
        except UnicodeDecodeError:
            return None, f"binary({codec})"
    else:
        for name in FALLBACK_ENCODINGS:
            try:
                text, codec = body.decode(name), name
                break
            except UnicodeDecodeError:
                continue
        else:
            return None, "unknown"

    return TextFile(text=text, encoding=codec, bom=bom), ""


def read_text(path: Path) -> tuple[str | None, str]:
    """老接口：只要 (正文, 标签)。不关心怎么写回去的调用方（explore/digest）用它。"""
    tf, err = load(path)
    if tf is None:
        return None, err
    return tf.text, tf.label


def write_back(path: Path, tf: TextFile, text: str) -> None:
    """按原编码 + 原 BOM 写回。编码不了就抛 UnicodeEncodeError，绝不悄悄换编码。"""
    path.write_bytes(tf.encode(text))


def save_shadow(path: Path, lines: list[str]) -> bool:
    """影子副本：只用于「外部改动后算 diff」，**刻意**统一成 UTF-8 + LF 存。

    为什么不用原编码原换行：影子副本只被 `split_lines()` 读进来做行级比对，
    Python 的 `splitlines()` 已经把 `\\r\\n` / `\\r` / `\\n` 都当成分隔符剥掉了，
    所以「CRLF 文件存成 LF 影子」不会让 diff 多出一行差异 —— 实测 CRLF 文件只改一行时
    diff 仍只报那一行。反过来按原编码存要多记一份 BOM/编码状态，收益为零、状态多一份。

    真正需要保真的是**被编辑的文件本身**，那走 `write_back()`，与这里无关。
    """
    try:
        CACHE_HOME.mkdir(parents=True, exist_ok=True)
        _shadow_path(path).write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception as exc:
        _LAST_SHADOW_ERROR = f"{type(exc).__name__}: {exc}"
        return False


_LAST_SHADOW_ERROR = ""


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_lines(text: str) -> list[str]:
    return text.splitlines()


def _emit(text: str, max_chars: int, kind: str) -> str | None:
    """输出文本。超长时落盘全文、只回「头 60% + 尾 30%」的预览。

    返回值：落盘路径（**只有真截断时才非 None**）。调用方必须处理这个返回值 ——
    只 print 不管的话，就会重演「输出砍了、账本却记成看全了」：下一次 `read` 拿
    sha1 一比发现没变，回一句「内容未变，此前已读 1 次」，于是一段从没进过对话的
    正文就被永久跳过了。这正是本项目要消灭的那类失败（答案在，但没被看见）。
    """
    if len(text) <= max_chars:
        print(text)
        return None
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
    return str(sp)


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
    tf, why = load(path)
    if tf is None:
        print(f"ERROR: 无法按文本读取（{why}）: {path}")
        return 2
    text, enc = tf.text, tf.label
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
        elif not changed_vs_cache and meta.get("read_count", 0) > 0 and meta.get("read_ranges"):
            # 只有**真记过区间**才允许走「内容未变」的免读分支。read_ranges 为空说明
            # 上次是被截断的（--max-lines 或单行超长），正文根本没进过上下文；
            # 此时回一句「已读 1 次」等于把那段内容永久跳过。
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
        recorded = [f"{r[0]}..{r[1]}" for r in (meta.get("read_ranges") or [])][-3:]
        # 没记下任何区间时**不能**说「最近读的范围: 全文」—— 那可能是上次被
        # --max-lines / 单行超长截断后留下的记录，全文从未进过上下文。
        where = f"已记录的区间: {recorded}" if recorded else "尚未有完整区间记录（上次可能被截断）"
        hint = (
            f"内容未变（sha1 相同）。此前已读 {meta.get('read_count')} 次；"
            f"{where}。需要正文请用 --mode range/full。"
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
            sp = _emit(d, args.max_chars, "diff")
            if rng:
                print(f"\n[变更行区间(新文件): {rng[0]}..{rng[1]}]  看局部: --mode range --start {rng[0]} --end {rng[1]}")
            if sp:
                # diff 本身被截断：影子副本仍会更到新内容，于是下一次 read 会说「内容未变」。
                # 必须现在就说清楚——只有溢出文件里有完整差异，别以为看完这段就等于看全了。
                print("\n[注意：这份 diff 没完整进入上下文，消失的那部分只在上面那个溢出文件里。"
                      "下一次 read 会说「内容未变」，不会再把这段差异给你。]")
            _bump(path, meta, disk, lines, enc, mode="changed", rng=rng, diff=d, version=next_version)
            return 0
        # 没有影子副本 → 老实给全文
        mode = "full"

    if mode == "range":
        # args.start 已由 argparse 的 `type=_positive_int` 保证 ≥1，这里只管补默认值
        s = args.start or 1
        e = min(total, args.end or min(total, s + 199))
        if s > total:
            print(f"ERROR: start={s} 超过总行数 {total}")
            return 2
        if e < s:
            print(f"ERROR: 区间反向（start={s} > end={e}）。文件共 {total} 行，"
                  f"想看末尾用: --mode range --start {max(1, total - 20)} --end {total}")
            return 2
        body = "\n".join(f"{i:5d}| {lines[i-1]}" for i in range(s, e + 1))
        out = f"{header}\n## 行 {s}..{e} / 共 {total} 行\n{body}"
        sp = _emit(out, args.max_chars, "read")
        if sp:
            # 区间行数不多但单行极长（压缩过的 JSON/bundle）时 _emit 仍会截断。
            # 这时不能再按「s..e 已读」记账，否则下次来读会被「内容未变」挡回去。
            print(f"\n[本区间未完整进入上下文，因此未记为已读。原文见上方 spill 文件，"
                  f"或缩小范围重读: --mode range --start {s} --end {max(s, e - 50)}]")
            _bump(path, meta, disk, lines, enc, mode="truncated", rng=None, version=next_version)
            return 0
        _bump(path, meta, disk, lines, enc, mode="range", rng=(s, e), version=next_version)
        return 0

    # full：受 --max-lines 约束，超了显式告知如何分页
    limit = args.max_lines or total
    show = lines[:limit]
    body = "\n".join(f"{i:5d}| {ln}" for i, ln in enumerate(show, 1))
    out = f"{header}\n{body}"
    sp = _emit(out, args.max_chars, "read")
    if sp or limit < total:
        # 两种情况都没把全文交给调用方：单行超长被 _emit 截断，或 --max-lines 截断。
        # 旧的记账按 (1, limit) 落 read_ranges 且照样 bump read_count —— 于是下一次
        # read 会理直气壮地说「内容未变，此前已读 1 次」，把没进过对话的正文跳过。
        # 现在改成 mode="truncated" + rng=None：read_count 仍计（这确实是次读），
        # 但读过的区间一笔不记。
        if sp:
            print(f"\n[全文未完整进入上下文，未记为已读。缩小范围重读: "
                  f"--mode range --start 1 --end {min(total, 200)}]")
        elif limit < total:
            print(f"\n[已显示前 {limit}/{total} 行，不算读过（重新 read 会再给一遍）。"
                  f"继续: --mode range --start {limit+1} --end {min(total, limit+300)}]")
        _bump(path, meta, disk, lines, enc, mode="truncated", rng=None, version=next_version)
        return 0
    _bump(path, meta, disk, lines, enc, mode="full", rng=(1, len(show)), version=next_version)
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
    if path.stat().st_size <= SHADOW_LIMIT and mode in ("full", "changed", "cached", "truncated"):
        # mode="truncated" 也要存影子副本：影子副本是给「下次内容变了算 diff」用的，
        # 跟这次打印了多少字无关。不存的话，大文件首次读后一旦被外部改动，
        # 下次只能降级回全文 —— 把本来能省的又还回去了。
        if not meta.get("shadow") or mode in ("full", "changed", "truncated"):
            if save_shadow(path, lines):
                meta["shadow"] = True
                meta.pop("shadow_error", None)
            else:  # 真失败了要知道，别静默降级
                meta["shadow"] = False
                meta["shadow_error"] = _LAST_SHADOW_ERROR
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
    tf, why = load(path)
    if tf is None:
        print(f"ERROR: 无法按文本编辑（{why}）: {path}")
        return 2
    text, enc = tf.text, tf.label
    old, oerr = _read_piece(args.old_file, args.old, "--old-file")
    if oerr:
        print(oerr)
        return 2
    if args.new_file:
        new, nerr = _read_piece(args.new_file, "", "--new-file")
        if nerr:
            print(nerr)
            return 2
    elif args.new is None:
        # 漏给 --new 时以前的 default="" 会把 old 静默替换成空，文件内容被删掉，
        # 而输出还说「已改」—— 这是最危险的一类：错得很安静、后果在别处才暴露。
        print("ERROR: 需要 --new/--new-file")
        print("  想删除这段内容，请显式传 --new ''（空串）。")
        return 2
    else:
        new = args.new
    if not old:
        print("ERROR: 需要 --old/--old-file")
        return 2

    # 换行宽容：调用方传片段时几乎总是 LF，而 Windows 上的文件多为 CRLF。
    # 不归一的话同一段文本也会匹配失败，逼调用方手动转换行符。
    # 两个方向都要管，而且 old / new 各归一各的：文件是 CRLF 时，即使 old 已经带
    # CRLF，new 里的裸 LF 也得换 —— 否则改完那行是 LF、邻行是 CRLF，文件变成混合换行。
    if "\r\n" in text:
        if "\r\n" not in old:
            old = old.replace("\r\n", "\n").replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n").replace("\r", "\n")

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
    if new_text == text:
        # --old 与 --new 相同时会走到这里。别报「已改」——那会让调用方以为改动了、
        # 进而跳过自己本该做的检查；缓存版本也不该为此加一。
        print(f"# 没变化：old 与 new 相同，{path.name} 原样未动（缓存版本未变）")
        return 0
    try:
        write_back(path, tf, new_text)
    except UnicodeEncodeError as exc:
        # GBK 文件里写进一个它编不了的字符（emoji、生僻字）—— 明确报错，
        # 而不是让它 half-write 或悄悄改成 UTF-8 把后面所有工具都搞坏。
        print(f"ERROR: 新内容无法用原编码 {tf.encoding} 编码：{exc}")
        print(f"  文件没被改动。要改用别的编码请用: fstate.py write {path} --encoding utf-8 ...")
        return 2
    except OSError as exc:
        print(f"ERROR: 写不进 {path}（{exc.strerror or exc}）")
        print("  文件没被改动。检查权限、或文件是否被别的程序占用。")
        return 2
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
        meta["shadow"] = save_shadow(path, new_lines)
        if not meta["shadow"]:
            meta["shadow_error"] = _LAST_SHADOW_ERROR
    _save_meta(path, meta)

    print(f"# 已改 {path}  → v{meta['version']}  {len(new_lines)} 行"
          + (f"  区间 {rng[0]}..{rng[1]}" if rng else ""))
    if args.reason:
        print(f"# reason: {args.reason}")
    # edit 的 diff 以前是裸 print(diff)：一次 --all 大改能刷出几十 KB，把上下文吃掉，
    # 而 write 早就有 --max-chars 兜着。同一个技能里两套规矩，最常用的那条偏偏没上限。
    sp = _emit(diff, args.max_chars, "diff")
    if sp:
        print(f"\n[注意：这次 diff 没完整进入上下文，被截掉的部分只在 {sp}。"
              f"文件本身已是新版，不用重读；要完整差异用 diff {args.path}。]")
    if rng:
        print(f"\n[确认改动不必重读全文：看局部用 --mode range --start {rng[0]} --end {rng[1]}]")
    return 0


# ---------------------------------------------------------------- write

def cmd_write(args: argparse.Namespace) -> int:
    """新建/整体覆盖：老内容存在就给 diff，不存在就只报行数与前几行。

    写回规格（编码 / BOM / 换行）优先级：`--encoding` > 原文件 > UTF-8 + LF。
    1.0.4 之前这一步恒定 `newline=""` + 探测出来的 "utf-8-sig"，于是
    覆盖 CRLF 文件会把整篇冲成 LF，覆盖无 BOM 文件会凭空长出一个 BOM。
    """
    path = resolve_input(args.path)
    if path.is_dir():
        print(f"ERROR: 这是目录不是文件: {path}")
        return 2
    if args.content_file:
        body, cerr = _read_piece(args.content_file, "", "--content-file")
        if cerr:
            print(cerr)
            return 2
    elif args.stdin:
        body = _read_stdin_text()
    else:
        print("ERROR: 需要 --content-file 或 --stdin")
        return 2

    old_lines: list[str] = []
    existed = path.is_file()
    original: TextFile | None = None
    if existed:
        original, why = load(path)
        if original is not None:
            old_lines = split_lines(original.text)
        else:
            print(f"# ⚠ 原文件无法按文本读取（{why}），将按新文件写")

    if args.encoding:
        codec = "utf-8" if args.encoding in ("utf-8-sig", "utf8") else args.encoding
        bom = codecs.BOM_UTF8 if args.encoding == "utf-8-sig" else b""
        nl = original.newline if original else ""
    elif original is not None:
        codec, bom, nl = original.encoding, original.bom, original.newline
    else:
        codec, bom, nl = "utf-8", b"", ""

    # 整篇覆盖是新内容，按目标文件风格统一换行；这是「保持原样」而不是「迁就调用方」。
    if nl:
        target_nl = "\r\n" if nl == "\r\n" else ("\r" if nl == "\r" else "\n")
        body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", target_nl)
    else:
        target_nl = "\n"
    if body and not body.endswith(("\n", "\r")):
        body += target_nl

    enc = codec + (" BOM" if bom else "") + (" CRLF" if target_nl == "\r\n" else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(bom + body.encode(codec))
    except UnicodeEncodeError as exc:
        print(f"ERROR: 内容无法用 {codec} 编码：{exc}")
        print("  文件没被改动。换编码请加 --encoding utf-8")
        return 2
    except OSError as exc:
        # 只读文件 / 目录不可写 / 被别的进程占着 —— 说清楚是哪个环节失败，
        # 别让用户对着一屏 Traceback 猜。
        print(f"ERROR: 写不进 {path}（{exc.strerror or exc}）")
        print("  文件没被改动。检查权限、或文件是否被别的程序占用。")
        return 2

    # 写完自检：重新读回来必须与写进去的一模一样，否则说明编码规格选错了 ——
    # 这类错误静默通过的话，下一个读它的工具（JSON.parse、解释器）才会炸，且离现场很远。
    check, why2 = load(path)
    if check is None or check.text != body:
        print(f"# ⚠ 写回自检未通过（{why2 or '内容不一致'}）：文件的字节层解读与写入内容不符")
        print(f"#   建议改编码: fstate.py write {path} --encoding utf-8 --content-file ...")

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
        meta["shadow"] = save_shadow(path, new_lines)
        if not meta["shadow"]:
            meta["shadow_error"] = _LAST_SHADOW_ERROR
    _save_meta(path, meta)

    head = "已改" if existed else "已新建"
    print(f"# {head} {path}  → v{meta['version']}  {len(new_lines)} 行  [{enc}]")
    if args.reason:
        print(f"# reason: {args.reason}")
    if diff:
        sp = _emit(diff, args.max_chars, "diff")
        if sp:
            # 改是真改了（字节已落盘），但证据没给全。让人别把「预览」当「全部」。
            print(f"\n[注意：这次 diff 没完整进入上下文，被截掉的部分只在 {sp}。"
                  f"文件本身已是新版，不用重读；要完整差异就看那个文件或用 diff {args.path}。]")
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
    scan_info: dict = {}
    for raw in args.paths:
        p = resolve_input(raw)
        if p.is_dir():
            for f in iter_files(p, args.max_scan, scan_info):
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
    total_avail = len(uniq)
    # 截断必须说出来：否则「20 个文件」会被读成「目录里就 20 个」，
    # 与之前修的「静默跳过非源码后缀」是同一类 bug，只是触发条件不同。
    uniq = uniq[: args.max_files]
    rest = total_avail - len(uniq)
    if not uniq:
        print("ERROR: 没有可摘要的文件（目录里没有常见源码后缀？可用 --max-files 放宽）")
        return 2

    if rest > 0:
        print(f"# 结构摘要  {total_avail} 个文件中的前 {len(uniq)} 个"
              f"（其余 {rest} 个看 --max-files，当前 {args.max_files}）")
    else:
        print(f"# 结构摘要  {len(uniq)} 个文件")
    if scan_info.get("capped"):
        print(f"# ⚠ 只扫描了前 {args.max_scan} 个文件（--max-scan），目录里还有没看的")
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
    if not rows:
        print("缓存里还没有记录（还没用 fstate read 过任何文件）")
        return 0
    print("读次数  行数   文件")
    for c, p, n in rows[: args.n]:
        flag = "  ← 反复读，考虑改用 range/diff" if c >= 5 else ""
        print(f"{c:6d}  {n:5d}   {p}{flag}")
    if len(rows) > args.n:
        print(f"（共 {len(rows)} 个文件有缓存记录，只显示前 {args.n} 个；看全部加 --n {len(rows)}）")
    return 0


# ---------------------------------------------------------------- explore

def iter_files(root: Path, max_files_scan: int, info: dict | None = None):
    """遍历可读源码文件。info 非 None 时，若因 max_files_scan 提前收手会置 capped=True——
    调用方必须把这件事说出来，否则「扫描 30 个」会被读成「目录里只有 30 个」。"""
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
                if info is not None:
                    info["capped"] = True
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
    scan_info: dict = {}
    for p in iter_files(root, args.max_scan, scan_info):
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
    if scan_info.get("capped"):
        print(f"  ⚠ 扫描在 {args.max_scan} 个文件处截断（--max-scan），更深的没看。没找到就调大它。")
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
    # allow_abbrev=False：否则 `--content hi` 会被 argparse 当成 `--content-file hi`
    # 的前缀缩写（--content 是 --content-file 的前缀），于是 "hi" 被当文件名去读，
    # 报一个跟真实原因毫无关系的 FileNotFoundError。前缀缩写只省几个字符，坑却很深。
    ap = argparse.ArgumentParser(prog="fstate.py", description="文件状态缓存 CLI", allow_abbrev=False)
    # subparsers 的 allow_abbrev 是**独立**的（3.8+ 才支持这个参数），这里的 --old/--new
    # 才是最容易写错的：`--old` 会先匹配上 `--old-file`，让 "--old abc" 变成
    # 「读 abc 这个文件当 old」。主 parser 关掉不管用，子 parser 必须各自关。
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("explore", allow_abbrev=False, help="一次成型探索")
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

    r = sub.add_parser("read", allow_abbrev=False, help="带缓存的读")
    r.add_argument("path")
    r.add_argument("--mode", default="auto", choices=["auto", "full", "range", "diff", "changed", "cached"])
    # 行号是 1-based。以前 --start 0 会被 max(1, ...) 悄悄改成第 1 行，于是
    # 「--start 0 --end 2」静默等价于「1..2」—— 调用方以为自己在读第 0 行往后的
    # 东西，实际偏移了一行还浑然不觉。宁可报错，不猜。
    r.add_argument("--start", type=_positive_line)
    r.add_argument("--end", type=_positive_line)
    r.add_argument("--max-lines", type=int, default=400)
    r.add_argument("--max-chars", type=int, default=20000)
    r.add_argument("--label", default="")
    r.set_defaults(fn=cmd_read)

    w = sub.add_parser("edit", allow_abbrev=False, help="编辑并回 diff")
    w.add_argument("path")
    w.add_argument("--old", default="")
    # default=None 而不是 ""：默认空串的话，调用方漏给 --new 时会被当成
    # 「把 old 替换成空」——明明是忘了参数，结果内容被静默删掉，diff 里还显示
    # 「已改」。设成 None 才能区分「没给」和「显式给了空串（=删除）」。
    w.add_argument("--new", default=None)
    w.add_argument("--old-file", default="")
    w.add_argument("--new-file", default="")
    w.add_argument("--all", action="store_true")
    w.add_argument("--reason", default="")
    w.add_argument("--label", default="")
    w.add_argument("--max-chars", type=int, default=12000)
    w.set_defaults(fn=cmd_edit)

    wr = sub.add_parser("write", allow_abbrev=False, help="新建/覆盖并回 diff")
    wr.add_argument("path")
    wr.add_argument("--content-file", default="")
    wr.add_argument("--stdin", action="store_true")
    wr.add_argument("--encoding", default="")
    wr.add_argument("--reason", default="")
    wr.add_argument("--label", default="")
    wr.add_argument("--max-chars", type=int, default=12000)
    wr.set_defaults(fn=cmd_write)

    s = sub.add_parser("state", allow_abbrev=False, help="文件状态")
    s.add_argument("path")
    s.set_defaults(fn=cmd_state)

    dg = sub.add_parser("digest", allow_abbrev=False, help="多文件结构摘要")
    dg.add_argument("paths", nargs="+")
    dg.add_argument("--max-files", type=int, default=20)
    dg.add_argument("--max-sigs", type=int, default=12)
    dg.add_argument("--max-scan", type=int, default=400)
    dg.add_argument("--all-ext", action="store_true", help="连非源码后缀（.md/.txt/无后缀）也摘要")
    dg.set_defaults(fn=cmd_digest)

    d = sub.add_parser("diff", allow_abbrev=False, help="最近一次 diff")
    d.add_argument("path")
    d.add_argument("--max-chars", type=int, default=12000)
    d.set_defaults(fn=cmd_diff)

    rs = sub.add_parser("reset", allow_abbrev=False, help="清缓存")
    rs.add_argument("path")
    rs.set_defaults(fn=cmd_reset)

    h = sub.add_parser("hot", allow_abbrev=False, help="读得最多的文件")
    h.add_argument("--n", type=int, default=15)
    h.set_defaults(fn=cmd_hot)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
