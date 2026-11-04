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


def _positive_int(raw: str) -> int:
    """argparse 的 type 校验：`--max-lines` / `--max-chars` 这类上限必须 ≥1。

    以前这些参数是裸 `type=int`，`--max-lines -5` 会被接受，于是输出
    「已显示前 -5/2 行…继续: --mode range --start -4 --end 2」—— 那条补救命令
    自己会被 _positive_line 拒掉，跑不通。上限没有负数这么一说。
    """
    try:
        val = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"得是整数，收到 {raw!r}") from None
    if val < 1:
        raise argparse.ArgumentTypeError(f"必须 ≥1，收到 {val}")
    return val


def _read_stdin_text() -> str:
    """读 stdin 的全部文本。

    Windows 上管道/重定向进来的是**系统 ANSI 页**的字节（本机 cp936），不是 UTF-8。
    `sys.stdin` 的默认解码器带 surrogateescape 时，中文会变成孤立代理字符：读的时候
    不报错，等 `body.encode("utf-8")` 才炸，错误位置离现场很远。所以这里绕过文本层，
    按字节读，显式按 utf-8-sig -> gbk 试。
    """
    raw = sys.stdin.buffer.read() if (sys.stdin is not None and getattr(sys.stdin, "buffer", None)) else None
    if raw is None:
        # cron / nohup / systemd / 某些 agent 运行器下 stdin 就是 None（或没有 buffer）。
        # 以前这里炸 AttributeError: 'NoneType' object has no attribute 'buffer'。
        raise OSError("没有可读的 stdin（stdin 已关闭或被剥离）")
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
    if file_arg and inline is not None and inline != "":
        # 两个都给了：以前静默挑文件那个，另一个连个响都没有。
        # 内联值和文件内容很可能完全不同，选错了改的就是别的东西。
        return "", (f"ERROR: {flag} 和它的内联形式同时给了，不知道该用哪个。"
                     "  二选一：要么只给内联值，要么只给文件。")
    if not file_arg:
        return inline, ""
    p = Path(file_arg)
    if not p.is_file():
        return "", f"ERROR: {flag} 指向的文件不存在: {file_arg}"
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return "", f"ERROR: 读不了 {flag}（{file_arg}）：{exc.strerror or exc}"
    # 报错信息里一直写着「不是 UTF-8/GBK 文本」，但实现只试过 utf-8-sig ——
    # 于是 GBK 片段会被判成读不了，而同字节走 --stdin 却能正确解码。
    # 声明了什么就得真做什么：utf-8-sig（顺手剥 BOM）→ gbk → latin-1，与 load() 同序。
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc), ""
        except UnicodeDecodeError:
            continue
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
# auto 落下来的全文读默认只给这么多行：默认值是为了别一次把上下文吃光。
# 显式 `--mode full` 不受这个默认值约束（那是在说「我就要整篇」）。
DEFAULT_MAX_LINES = 400
#: 缓存写入失败只提示一次（一个进程一回合里反复喊同一句只会占篇幅）。
_CACHE_WRITE_FAILED = False


def _session_id() -> str:
    """当前会话标识，用于判断「这份正文还在本次上下文里吗」。

    账本存在磁盘上、是跨进程持久的；但「正文曾经进过上下文」这件事只在**同一个会话**
    里成立。上下文压缩、换会话、重启之后，正文早已不在上下文里，而账本还以为在，
    于是 `read` 会回一句「内容未变」把正文永久挡在门外 —— 这正是本工具要消灭的失败模式，
    却由它自己制造。

    DSH 会给 bash 工具注入 `DSH_SESSION_ID`，有就用它。

    拿不到就返回空串 —— 而空串在调用方那里**必须**被当成「不是同一会话」（保守 =
    重新给正文）。这条纪律曾经写反过：`same_session` 里有个 `or (not sess)`，
    于是「拿不到会话标识」反而变成了「无条件算同一会话」，跟这里的承诺正好相反。
    实测去掉 `DSH_SESSION_ID` 后，`read` 照样回「内容未变…本会话已完整交付过」。

    注意 `os.environ.get("PPID")` 是拿不到的：PPID 是 shell 的内建变量，不导出到
    子进程的环境里。所以只剩一级，别再写 `pid-{PPID}` 这种永远走不到的分支，
    那只会让人以为还有一层兜底。
    """
    return (os.environ.get("DSH_SESSION_ID") or "").strip()



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


_LAST_META_BAD = ""


def _meta_sane(m: object) -> tuple[bool, str]:
    """缓存记录的结构校验。

    以前只 catch 了 json 解码错误。于是「合法 JSON 但结构不对」（`[]`、或者
    `{"version":[1,2],"read_ranges":"notalist"}`）会一路带到业务逻辑里，变成
    `AttributeError: 'list' object has no attribute 'get'` 和
    `TypeError: object of type 'int' has no len()` —— 三处裸栈，
    而 `state` 那会儿还会说「无缓存」，把唯一的排查线索掐掉。
    """
    if not isinstance(m, dict):
        return False, f"顶层不是对象（是 {type(m).__name__}）"
    # bool 是 int 的子类：`isinstance(True, int)` 为真，于是 `{"version": true}`
    # 能过校验，接着 `v{version}` 打出「版本 vTrue」、`full_version == version`
    # 也成立（True == 1），凭证被伪造出来。整数就用整数，别用真假值。
    for k in ("version", "read_count", "lines"):
        if k in m and m[k] is not None and (isinstance(m[k], bool) or not isinstance(m[k], int)):
            return False, f"字段 {k} 不是整数"
    for k in ("full_version",):
        if k in m and m[k] is not None and (isinstance(m[k], bool) or not isinstance(m[k], int)):
            return False, f"字段 {k} 不是整数"
    # last_read_at / mtime 存的是 time.time()，**浮点**。把它们也要求成 int，
    # 结果是每一条刚写下的缓存都在下次读取时被判「损坏并丢弃」——
    # 凭证当场失效，而报错理由是「字段 last_read_at 不是整数」，跟真相完全对不上。
    for k in ("last_read_at", "mtime"):
        if k in m and m[k] is not None and (
            isinstance(m[k], bool) or not isinstance(m[k], (int, float))
        ):
            return False, f"字段 {k} 不是数字"
    for k in ("read_ranges", "last_edit_range"):
        if k in m and m[k] is not None and not isinstance(m[k], list):
            return False, f"字段 {k} 不是列表"
    if "path" in m and not isinstance(m["path"], str):
        return False, "字段 path 不是字符串"
    # 区间只校验**结构**，不校验越界。
    # 「文件被外部改短」是正常事件（100 行的文件被改成 5 行），旧区间 1..100 自然越界，
    # 那不是缓存坏了。早先这里把越界也判成损坏，于是每次外部改短都会把整条记录丢掉：
    # 版本号回退成 v1、区间照旧显示 1..100，而且 state 会嘴硬说「缓存条目已损坏」。
    # 越界项在 `_bump` 与 `cmd_state` 里按当前行数裁剪即可。
    lines = m.get("lines")
    bad_ranges = list(m.get("read_ranges") or [])
    if m.get("last_edit_range"):
        bad_ranges.append(m["last_edit_range"])
    for r in bad_ranges:
        if not (isinstance(r, list) and len(r) == 2):
            return False, "区间不是 [起, 止] 两个整数"
        for x in r:
            if isinstance(x, bool) or not isinstance(x, int):
                return False, "区间端点不是整数"
    return True, ""


def _load_meta(path: Path) -> dict:
    global _LAST_META_BAD
    _LAST_META_BAD = ""
    p = _meta_path(path)
    if not p.is_file():
        return {}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        _LAST_META_BAD = f"读不出来（{type(exc).__name__}）"
        return {}
    ok, why = _meta_sane(m)
    if not ok:
        # 坏条目直接丢掉，别让它继续毒后面的每一次 read/hot/diff。
        _LAST_META_BAD = why
        try:
            p.unlink()
        except OSError:
            pass
        return {}
    return m


def _save_meta(path: Path, meta: dict) -> None:
    """写缓存记录。**失败不许炸**。

    缓存是加速层，不是数据。以前这里让 `mkdir` / `write_text` 的异常裸奔：
    FSTATE_HOME 指向一个普通文件（磁盘满、误配置、同步盘冲突都会这样）、
    或者某个 {key}.json 被换成了目录，都会在**正文已经正常打印之后**
    抛 FileExistsError / PermissionError，退出码变成 1。
    调用方看到 exit 1 会以为「读失败了」，于是重试或换工具 —— 白花一次往返，
    而它其实早就拿到全文了。所以：写不成就写不成，安静地降级。
    """
    try:
        CACHE_HOME.mkdir(parents=True, exist_ok=True)
        _meta_path(path).write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    except OSError as exc:
        # 只提示一次，且明确说「正文没问题」——否则这行警告本身就成了新的困惑源。
        global _CACHE_WRITE_FAILED
        if not _CACHE_WRITE_FAILED:
            _CACHE_WRITE_FAILED = True
            print(f"# 缓存写不进去（{exc.strerror or exc}）: {CACHE_HOME}")
            print("# 不影响这次输出，只是这次读不会被记住（下次还会重新给你正文）。")


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

    # BOM 只能决定**候选**解码器，不能一命中就定论。UTF-32-LE 的 BOM（ff fe 00 00）
    # 是 UTF-16-LE BOM（ff fe）的超集：一个正文以 U+0000 开头的合法 UTF-16-LE 文件
    # 会被误认成 UTF-32-LE，解码失败后被判成 binary 并从此读不出来 —— 而它完全合法。
    # 所以按「长前缀优先」收集全部候选，挨个试，第一个解出文本的算数。
    candidates = [(marker, name) for marker, name in BOM_TABLE if raw.startswith(marker)]

    if not candidates:
        # 只有「无 BOM」的文件才用 NUL 判二进制；UTF-16/32 正文里满是 NUL，
        # 但它们已经被上面的候选认领走了。
        if b"\x00" in raw[:8192]:
            return None, "binary"
        for name in FALLBACK_ENCODINGS:
            try:
                return TextFile(text=raw.decode(name), encoding=name, bom=b""), ""
            except UnicodeDecodeError:
                continue
        return None, "unknown"

    last_err = "unknown"
    for bom, codec in candidates:
        try:
            return TextFile(text=raw[len(bom):].decode(codec), encoding=codec, bom=bom), ""
        except UnicodeDecodeError:
            last_err = f"binary({codec})"
            continue
    return None, last_err


def _broken_link_hint(path: Path) -> str:
    """路径存在但读不到时，区分「链接坏了」和「压根没这个文件」。"""
    if path.is_symlink() and not path.exists():
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
        return f"这是悬挂软链接（指向 {target}，但目标不存在）"
    return ""


def read_text(path: Path) -> tuple[str | None, str]:
    """老接口：只要 (正文, 标签)。不关心怎么写回去的调用方（explore/digest）用它。

    读失败（被独占、没权限）也返回 (None, 原因) 而不是抛 —— explore/digest
    是拿去「在陌生仓库里摸底」的，一个被锁住的 .log/.db 不该让整次摸底失败。
    """
    try:
        tf, err = load(path)
    except OSError as exc:
        return None, f"读不了（{exc.strerror or exc}）"
    if tf is None:
        return None, err
    return tf.text, tf.label


def _write_real_target(path: Path) -> Path:
    """返回「真正该被写的那个文件」。

    `_atomic_write` 用 `os.replace(tmp, path)` 换文件。这在普通文件上完全正确，
    但作用在链接上会把链接本身换掉：
      - 软链接 → 变成普通文件，链接目标一个字节都没变（「改了却什么都没改」）；
      - 硬链接 → nlink 从 2 掉到 1，两个名字从此分家（另一边的改动静默丢失）。
    这个坑是原子写**引入的回归** —— 旧的 `write_bytes` 反而是跟随链接的。
    防止数据损坏的修复不能自己制造数据损坏，所以这里沿 realpath 写。
    """
    return Path(os.path.realpath(path))


def _atomic_write(path: Path, data: bytes) -> None:
    """原子写：先写同目录临时文件，再 os.replace 换上去。

    直接 `path.write_bytes(data)` 会先以 `wb` 打开 —— **那一刻原文件已经被截成 0 字节**。
    于是「写得慢 / 中途出错 / 进程被 kill / 磁盘满了」任何一个环节出事，用户拿到的
    都是一个空文件或半截文件，而原内容已经没了。实测 20KB 的源文件被一个 80MB 的
    `--content-file` 覆盖时 kill -9，文件变成 83,600,000 字节且原 1000 行全丢。

    本工具的主业是改用户的源代码，没有任何理由让它冒这个风险：
    临时文件写完 + fsync + `os.replace()`（同盘上是原子操作），失败时原文件一个字节都不动。
    """
    path = _write_real_target(path)
    # 硬链接：os.replace 会把这个名字指向新 inode，nlink 从 2 掉回 1，
    # 两个名字从此分家 —— 另一个名字下的改动静默丢失（实测 o.txt 仍是旧内容）。
    # 硬链接的语义就是「同一个 inode 的多个名字」，所以对它只能原地写回。
    # 代价是失去原子性，但内容已经在内存里、写之前不会截断原文件之外的东西；
    # 而且这本来就是用户选择硬链接时接受的语义。软链接已经在上面跟随到 realpath 了。
    try:
        linked = path.stat().st_nlink > 1
    except OSError:
        linked = False
    if linked:
        with open(path, "r+b") as fh:
            fh.write(data)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
        return
    # 临时文件名必须唯一：写 20MB 内容时被 kill，会留下一个等于整个新内容的
    # `.x.fstate-tmp`（实测 20,402,886 字节），而固定名字意味着下一次运行既不会
    # 清理它，又可能和另一个并发进程互相踩。带上 pid + 随机后缀，同时顺手清掉
    # 同一个目标路径上超过一小时的陈旧残留（正常用完就没了，留下的都是崩溃遗留）。
    dir_ = path.parent
    try:
        now = time.time()
        for old_tmp in dir_.glob(f".{path.name}.fstate-tmp*"):
            try:
                if now - old_tmp.stat().st_mtime > 3600:
                    old_tmp.unlink()
            except OSError:
                pass
    except OSError:
        pass
    tmp = dir_ / f".{path.name}.fstate-tmp-{os.getpid()}-{os.urandom(3).hex()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Windows 上 `os.replace` 遇到「目标正被另一个进程替换」会报
        # PermissionError（拒绝访问），但那是**瞬时**的共享冲突，不是权限问题。
        # 实测 20 个进程同时 write 同一个文件，6 个栽在这里，文案却劝用户
        # 「检查权限、或文件是否被别的程序占用」—— 把一个能自己重试掉的事故
        # 说成了用户的环境问题。多 agent 并行改同一个文件是真实场景，得兜住。
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except OSError:
        # 收拾残局，别把半成品临时文件留在用户目录里。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_back(path: Path, tf: TextFile, text: str) -> None:
    """按原编码 + 原 BOM 写回。编码不了就抛 UnicodeEncodeError，绝不悄悄换编码。"""
    _atomic_write(path, tf.encode(text))


_LAST_SHADOW_ERROR = ""


def save_shadow(path: Path, lines: list[str]) -> bool:
    """影子副本：只用于「外部改动后算 diff」，**刻意**统一成 UTF-8 + LF 存。

    为什么不用原编码原换行：影子副本只被 `split_lines()` 读进来做行级比对，
    Python 的 `splitlines()` 已经把 `\\r\\n` / `\\r` / `\\n` 都当成分隔符剥掉了，
    所以「CRLF 文件存成 LF 影子」不会让 diff 多出一行差异 —— 实测 CRLF 文件只改一行时
    diff 仍只报那一行。反过来按原编码存要多记一份 BOM/编码状态，收益为零、状态多一份。

    真正需要保真的是**被编辑的文件本身**，那走 `write_back()`，与这里无关。
    """
    global _LAST_SHADOW_ERROR
    try:
        CACHE_HOME.mkdir(parents=True, exist_ok=True)
        _shadow_path(path).write_text("\n".join(lines), encoding="utf-8")
        _LAST_SHADOW_ERROR = ""
        return True
    except Exception as exc:
        # 缺 `global` 时这里是给一个函数局部变量赋值，模块级的那份永远是 ""，
        # 于是调用方拿到的「失败原因」始终是空串，而注释还写着「真失败了要知道，别静默降级」。
        _LAST_SHADOW_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def _load_shadow(path: Path, meta: dict) -> tuple[list[str] | None, str]:
    """读影子副本，并确认它没被写坏。返回 (旧行, 不可用原因)。

    影子副本是纯文本**覆盖写**。写到一半被打断、被清理工具截掉尾巴、被同步盘
    截断，都会留下一个「前半段还在」的文件。它看起来完全正常 —— 于是拿它跟
    完整的新版本做 diff，后半段会被统统标成「新增」：真实只改了第 15 行，
    工具却报「6..20 全是新增」，还据此让调用方去 `--mode range --start 6 --end 20`。
    行数对不上就直接判废，退回「没有旧版本快照」这条路（给全文并说明原因）。
    """
    shadow = _shadow_path(path)
    if not shadow.is_file() or meta.get("shadow") is not True:
        return None, (meta.get("shadow_error") or "缓存里没有旧版本快照")
    try:
        old_lines = split_lines(shadow.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"旧版本快照读不出来（{type(exc).__name__}）"
    want = meta.get("lines")
    if isinstance(want, int) and want >= 0 and len(old_lines) != want:
        return None, (f"旧版本快照不完整（记着 {want} 行，实际只有 {len(old_lines)} 行，"
                      f"可能是写入被打断）")
    return old_lines, ""


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_lines(text: str) -> list[str]:
    """按「真正的换行」切行，只认 \\r\\n、\\r、\\n。

    别用 str.splitlines()：它把 \\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028 \\u2029
    也算行分隔符。Python 源码里的换页符 \\x0c 很常见，一旦被当成换行，
    行号就和编辑器/编译器对不上 —— 而本工具最拿得出手的东西恰恰是
    「回精确行号区间」，行号错了比不给更糟。
    """
    if text == "":
        return []
    parts = re.split(r"\r\n|\r|\n", text)
    # 末尾那个换行符不该凭空多出一行：'a\n' 是 1 行，不是 2 行。
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _unique_spill(spill_dir: Path, kind: str) -> Path:
    """给溢出文件取一个当期唯一的名字。

    以前是 `{年-月-日-时:分:秒}-{kind}.txt`：同一秒内两次溢出（连读两个大文件、
    读一个再 diff 一个）会撞名，后写的把先写的覆盖掉，而两次提示里都理直气壮地
    让调用方「去那个路径看全文」—— 第一个文件的全文就这么没了，且没有任何痕迹。
    加 pid + 自增序号，撞了就再往后找，宁可名字丑一点。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        name = f"{stamp}-{kind}" + (f"-{n}" if n else "") + ".txt"
        sp = spill_dir / name
        if not sp.exists():
            return sp
        n += 1


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
    sp = _unique_spill(spill_dir, kind)
    sp.write_text(text, encoding="utf-8")
    # --max-chars 3 这种极小值时，int(3*0.3)==0，`text[-0:]` 是**整个字符串**，
    # 于是「截断预览」反而把全文都打了出来，还打印「已截断 -1 字符」。
    # 两份都至少给 1 个字符，且保证 head+tail 不超过预算。
    head_n = max(1, min(len(text), int(max_chars * 0.6)))
    tail_n = max(1, min(len(text) - head_n, int(max_chars * 0.3)))
    if head_n + tail_n > max_chars:
        tail_n = max(0, max_chars - head_n)
    head = text[:head_n]
    tail = text[-tail_n:] if tail_n > 0 else ""
    hidden = len(text) - len(head) - len(tail)
    print(head)
    print(f"\n... [已截断 {hidden} 字符] ...\n")
    if tail:
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
    # msys/git-bash 风格的 `/c/tmp/x`：Windows 上 Path('/c/tmp/x').is_absolute() 是
    # **False**，于是会被 Path.cwd() 拼成 `C:\\c\\tmp\\x` —— 写到另一个位置还报成功，
    # 而 FSTATE_HOME=/c/... 又能正常工作（msys 自己转换了），很容易让人以为哪儿都能用。
    # 直接把单字母盘符前缀转成 X:，与 git-bash 的语义对齐。
    s = str(path)
    m = re.match(r"^/([A-Za-z])(/.*)?$", s)
    if m and os.name == "nt":
        path = Path(f"{m.group(1).upper()}:{m.group(2) or '/'}")
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
        hint = _broken_link_hint(path)
        print(f"ERROR: {hint or '文件不存在'}: {path}")
        if hint:
            print("  链接本身还在，只是目标没了；把目标文件恢复、或删掉这个链接再试。")
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
    sess = _session_id()
    # 「正文已经完整交付过」= 有过一次未截断的全文读，且那次的版本号就是当前版本。
    # 这是 save/免读的**唯一**凭证。以前判的是「read_ranges 非空」，于是只读过
    # 第 5..10 行也算「读过」，auto 就回「内容未变」把 70% 的正文永久跳过；
    # edit/write 改了行数还照样有效，跨会话也照样有效。
    body_delivered = (
        meta.get("full_version") is not None
        and meta.get("full_version") == meta.get("version")
    )
    # 账本是磁盘级的、持久化的；「正文在上下文里」是对话级的、会随会话结束和
    # 上下文压缩蒸发。两者作用域不同，所以必须记下交付时是哪个会话。
    # 拿不到会话标识（空串）时按**保守**处理：不算同一会话，重新给正文。
    # 以前这里写的是 `(not meta.get("session")) or (not sess) or ...`，两个「缺值」
    # 都被当成「相等」—— 结果是最该保守的场合反而最宽松（实测：清掉
    # DSH_SESSION_ID 后仍然回「内容未变」）。缺值只有一种安全的解释方式。
    same_session = bool(sess) and meta.get("session") == sess

    if mode == "auto":
        # 显式给了行范围就是想要局部正文，别拿「内容未变」把请求挡回去
        if args.start is not None or args.end is not None:
            mode = "range"
        elif changed_vs_cache:
            mode = "changed"
        elif body_delivered and same_session:
            mode = "cached"
        else:
            mode = "full"

    next_version = (meta.get("version") or 0)
    if first_time:
        next_version = 1
    elif changed_vs_cache:
        next_version += 1
    # --label 只影响 diff 里的 a/ b/ 前缀（去 diff 对比过，header 一直是绝对路径）。
    # 这是有意的：header 里的绝对路径是唯一能让人立刻定位到磁盘上哪个文件的信息，
    # 被一个自定标签顶掉反而更糊涂。想看相对路径就靠 rel。
    shown = f"{args.label}  ({path})" if args.label else path
    header = f"# {shown}  版本 v{next_version}  {total} 行  [{enc}]"

    if mode == "cached":
        # 显式 --mode cached 也会走到这里，此时**可能一条凭证都没有**
        # （从没读过、或换了个会话、或文件刚被改过）。
        # 以前这里闭着眼睛说「本会话已完整交付过 vNone 的正文（此前调用 None 次）」，
        # 既没正文又给了个假保证 —— 正是本工具要消灭的那种话。
        #
        # 注意第一个条件 `changed_vs_cache`：`body_delivered` 比的是**缓存里**的
        # full_version 与 version，磁盘变了它照样成立（v1 == v1）。于是
        # 「首读 old → 外部改成 new → read --mode cached」会输出
        # `版本 v2` + `内容未变（sha1 相同）` —— 同一个文件头说 v2、正文说没变，
        # 这句「sha1 相同」是纯假的。磁盘变了就不是 cached，先判这个。
        if changed_vs_cache or not (body_delivered and same_session):
            print(header)
            if changed_vs_cache:
                print("内容已变（磁盘 sha1 与缓存不同），缓存里记的是上一次的内容，"
                      "所以这次不算「未变」。要正文请用 --mode full/range，"
                      "或 --mode changed 只要差异。")
            else:
                print("本会话还没有完整交付过这份正文"
                      "（可能是从没读过、或换了会话/上下文已重置）。"
                      "要正文请用 --mode full 或 --mode range。")
            return 0
        # 走到这里意味着：磁盘没变 + 本次会话里完整交付过当前版本的正文。
        recorded = [f"{r[0]}..{r[1]}" for r in (meta.get("read_ranges") or [])][-3:]
        print(header)
        print(f"内容未变（sha1 相同），且本会话已完整交付过 v{meta.get('version')} 的正文 "
              f"（此前调用 {meta.get('read_count')} 次）。需要正文请用 --mode range/full。")
        if recorded:
            # 区间只是历史记录，不是「你这里有正文」的凭证 —— 明说，别让人误以为
            # 列出的范围之外的内容也在上下文里。
            print(f"（历史读过（仅供参考）: {recorded}）")
        if meta.get("last_edit_range"):
            print(f"上次编辑区间: {meta['last_edit_range'][0]}..{meta['last_edit_range'][1]}")
        # 这次一个字的正文都没交付，不该计一次「读」。
        return 0

    if mode == "changed" and not changed_vs_cache:
        # 显式要「只要差异」，但磁盘压根没变。以前这里静默落到全文分支，
        # 于是一句「给我差异」换来一整篇正文 + 账本记成已读全文。
        print(header)
        if not meta.get("sha1"):
            # 「没变」的前提是「以前看过」。从没读过的时候一个基准都没有，
            # 说「与上次读时相同」是拿不存在的东西当参照物。
            print("还没有这份文件的任何记录（从没读过），没有基准可比。"
                  "要正文请用 --mode full/range。")
        else:
            print("磁盘内容与上次读时相同，没有差异。要正文请用 --mode full/range。")
        return 0

    if mode == "diff":
        # 以前 --mode diff 只在 choices 里有名字，cmd_read 里没有对应分支，
        # 于是它静默落到 full：要差异的人得到整篇正文，还被记成「已读全文」。
        old_lines, why = _load_shadow(path, meta)
        if old_lines is None:
            print(f"# 没有可用的旧版本快照（{why}），给不了差异。要正文请用 --mode full/range。")
            return 0
        d = unified(old_lines, lines, rel)
        if not d.strip():
            # 「没有行级差异」有两种完全不同的原因，以前一律说成后者：
            # ① 刚用本脚本 edit/write 过 —— 那时影子副本已经被同步成新内容，
            #    拿它比磁盘自然一模一样。这是**正常的**，却被说成「字节层可能变了」，
            #    等于把「我自己的写入覆盖了基准」栽赃给文件的换行/BOM/编码。
            # ② 读完之后磁盘确实没变，只有字节层（换行/BOM/编码）动了。
            if not changed_vs_cache and meta.get("last_edit_at"):
                print(f"# 行内容没有变化（版本 v{meta.get('version', 0)}，{total} 行）。")
                print("  缓存里的基准已同步成当前内容（上次是本脚本自己的 edit/write 改的），"
                      "所以比不出差异。")
                print(f"  那次改了什么用: fstate.py diff {args.path}")
            else:
                print(f"# 行内容没有变化（版本 v{meta.get('version', 0)}，{total} 行）。"
                      f"字节层可能变了（换行/BOM/编码），用 diff 看不了这个。")
            return 0
        print(header)
        print(f"## 与上次读到的版本（v{meta.get('version', 0)}）的差异：\n")
        sp = _emit(d, args.max_chars, "diff")
        if sp:
            print(f"\n[注意：这份 diff 没完整进入上下文，完整内容只在 {sp}。]")
        # 只交付了差异，没交付全文 —— 绝不能让「已读全文」的凭证继续有效。
        _bump(path, meta, disk, lines, enc, mode="changed", rng=None, total=total,
              diff=d, version=next_version)
        return 0

    if mode == "changed" and changed_vs_cache:
        old_lines, why_no_diff = _load_shadow(path, meta)
        if old_lines:
            d = unified(old_lines, lines, rel)
            rng = changed_lines_range(old_lines, lines) or hunks_new_range(d)
            print(header)
            print(f"## 磁盘内容已变（v{meta.get('version', 0)+1}），以下仅为差异：\n")
            sp = _emit(d, args.max_chars, "diff")
            if rng:
                print(f"\n[变更行区间(新文件): {rng[0]}..{rng[1]}]  "
                      f"看局部: --mode range --start {rng[0]} --end {rng[1]}")
            if sp:
                print("\n[注意：这份 diff 没完整进入上下文，消失的那部分只在上面那个溢出文件里。"
                      "下一次 read 不会再说「内容未变」——你没看全的部分，它会重新给你。]")
            # 关键：差异不是全文。以前这里把「差异落在哪几行」写进 read_ranges 当已读凭证，
            # 于是 500 行文件里改 1 行、diff 只给 7 行，账本却让剩下 493 行永远跳过。
            _bump(path, meta, disk, lines, enc, mode="changed", rng=None, total=total,
                  diff=d, version=next_version)
            return 0
        # 没有影子副本（或它已损坏）→ 只能老实给全文。但得说清楚为什么给不了差异：
        # 否则「我说要 diff，它却甩全文」看起来像没听懂，下次还这么用。
        print(f"# 没有可用的旧版本快照（{why_no_diff}），给不了差异，下面是全文"
              f"（超过 {SHADOW_LIMIT // 1024 // 1024} MB 的文件本就不存影子副本）。")
        mode = "full"

    if mode == "range":
        # args.start 已由 argparse 的 `type=_positive_line` 保证 ≥1，这里只管补默认值
        s = args.start or 1
        e = min(total, args.end or min(total, s + 199))
        if s > total:
            print(f"ERROR: start={s} 超过总行数 {total}")
            return 2
        if e < s:
            print(f"ERROR: 区间反向（start={s} > end={e}）。文件共 {total} 行，"
                  f"想看末尾用: --mode range --start {max(1, total - 20)} --end {total}")
            return 2
        # 显式 --max-lines 在 range 里同样要算数：以前这个分支完全不看它，
        # `--mode range --start 1 --end 200 --max-lines 10` 照样吐 200 行、
        # 一个字不解释 —— 同一个参数在 full 里生效、在 range 里被静默吞掉。
        want_lines = e - s + 1
        if args.max_lines is not None and want_lines > args.max_lines:
            e = s + args.max_lines - 1
        body = "\n".join(f"{i:5d}| {lines[i-1]}" for i in range(s, e + 1))
        out = f"{header}\n## 行 {s}..{e} / 共 {total} 行\n{body}"
        sp = _emit(out, args.max_chars, "read")
        if args.max_lines is not None and want_lines > args.max_lines:
            print(f"\n[只给了 {s}..{e}（--max-lines {args.max_lines}），"
                  f"你要的还差 {want_lines - (e - s + 1)} 行。"
                  f"继续: --mode range --start {e + 1} --end {s + want_lines - 1}]")
        if sp:
            # 区间行数不多但单行极长（压缩过的 JSON/bundle）时 _emit 仍会截断。
            # 这时不能再按「s..e 已读」记账，否则下次来读会被「内容未变」挡回去。
            print(f"\n[本区间未完整进入上下文，因此未记为已读。原文见上方 spill 文件，"
                  f"或缩小范围重读: --mode range --start {s} --end {max(s, e - 50)}]")
            _bump(path, meta, disk, lines, enc, mode="truncated", rng=None, total=total,
                  version=next_version)
            return 0
        _bump(path, meta, disk, lines, enc, mode="range", rng=(s, e), total=total,
              version=next_version)
        return 0

    # full：显式 --mode full 的语义就是「整篇」，不能被 --max-lines 的默认值推翻；
    # auto 落下来的 full 才走默认上限（默认值是为了别把上下文一次吃光）。
    if args.max_lines is not None:
        limit = args.max_lines
    elif args.mode == "full":
        limit = total
    else:
        limit = min(total, DEFAULT_MAX_LINES)
    show = lines[:limit]
    body = "\n".join(f"{i:5d}| {ln}" for i, ln in enumerate(show, 1))
    out = f"{header}\n{body}"
    sp = _emit(out, args.max_chars, "read")
    if sp or limit < total:
        if sp:
            print(f"\n[全文未完整进入上下文，未记为已读。缩小范围重读: "
                  f"--mode range --start 1 --end {min(total, 200)}]")
        else:
            print(f"\n[已显示前 {limit}/{total} 行，不算读过（重新 read 会再给一遍）。"
                  f"继续: --mode range --start {limit+1} --end {min(total, limit+300)}]")
        _bump(path, meta, disk, lines, enc, mode="truncated", rng=None, total=total,
              version=next_version)
        return 0
    _bump(path, meta, disk, lines, enc, mode="full", rng=(1, total), total=total,
          version=next_version)
    return 0


def _bump(path: Path, meta: dict, disk: str, lines: list[str], enc: str,
          *, mode: str, rng, diff: str | None = None, version: int | None = None,
          total: int | None = None) -> None:
    now = time.time()
    # 只有这次真的把整篇正文交出去了，才发「已读全文」的凭证。
    # mode="full" 且 rng 覆盖 1..total（调用方在下面按 len(show)==total 传入）。
    delivered_full = mode == "full" and rng is not None and total is not None and list(rng) == [1, total]
    sess = _session_id()

    ranges = meta.get("read_ranges") or []
    if rng:
        r = list(rng)
        # 连着读同一段（最常见：全文读两遍）没必要在列表里堆成
        # [[1,1],[1,1],[1,1]]，那样「已记录的区间」提示会变成一串重复噪音。
        if not ranges or ranges[-1] != r:
            ranges.append(r)
    # 区间按当前行数裁剪：文件被外部改短时，旧区间（比如 100 行文件记下的 1..100）
    # 会一路显示成「历史读过 1..100」，而文件现在只有 5 行 —— 那是凭空造出来的行号。
    n_now = len(lines)
    clipped: list[list[int]] = []
    for r in ranges:
        if not (isinstance(r, list) and len(r) == 2):
            continue
        a, b = max(1, int(r[0])), min(n_now, int(r[1]))
        if a <= b:
            clipped.append([a, b])
    ranges = clipped
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
        "session": sess,
    })
    # 免读凭证：只有「整篇正文真的进了这次上下文」才发，且绑定版本号。
    # 以前判的是「read_ranges 非空」—— 那个字段记的是「读到过哪些行」，局部读也有值，
    # 于是 auto 把「读过第 5..10 行」当成「读过整个文件」，70% 的正文永远不会再被送到
    # 上下文里；edit/write 改了行数、换了整篇内容，那个字段还在，凭证也还在。
    # 现在的规则简单：全文交付一次 → full_version = 当时的版本；之后版本一变自动过期。
    if delivered_full:
        meta["full_version"] = version
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
        hint = _broken_link_hint(path)
        print(f"ERROR: {hint or '文件不存在'}: {path}")
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
        new, nerr = _read_piece(args.new_file, args.new, "--new-file")
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
    #
    # 但「文件里出现过 CRLF」≠「全文都是 CRLF」。以前就是这么当的：只要 text 里有
    # 一个 \r\n，就把 old/new 整块转成 CRLF。于是混合换行文件（Windows 上编辑过、
    # 又被 git autocrlf 碰过的文件非常常见）里 `ccc\nddd` 这段真实字节是 LF，
    # 两个变体都对不上 —— 任何跨行 edit 永远失败，而报错还把人引向「用行号微调」。
    #
    # 反过来也同样错：文件是混合换行时把 new 全转成 CRLF，会在原本是 LF 的位置
    # 插进 CRLF，diff 里看不出来（diff 只按行比），文件却越来越碎。
    # 现在：挨个变体试匹配，谁命中就照谁的换行风格去写 new。
    variants: list[str] = [old]
    for v in (old.replace("\r\n", "\n").replace("\n", "\r\n"),
              old.replace("\r\n", "\n")):
        if v not in variants:
            variants.append(v)
    matched = next((v for v in variants if v in text), None)
    if matched is None:
        count = 0
    else:
        old = matched
        count = text.count(old)
        if "\r\n" in matched:
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        else:
            new = new.replace("\r\n", "\n").replace("\r", "\n")
            # 单一换行风格的文件：单行命中也要跟随全文，否则又是一处混合
            if tf.newline == "\r\n" and not tf.mixed_newline:
                new = new.replace("\n", "\r\n")

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
    # 编辑前先记下「我读到的是什么版本」和「磁盘现在是什么」。
    # 两者不同 → 在我上次读完之后，有别人动过这个文件。
    meta = _load_meta(path)
    pre_sha = sha1_of(path)
    cache_sha = meta.get("sha1")
    external_changed = bool(cache_sha) and cache_sha != pre_sha
    # 外部改过时，diff 的「旧版本」应该是我上次读到的那个版本（影子副本），
    # 而不是刚刚读进内存的磁盘现状 —— 否则别人的改动会被 diff 静默吞掉：
    # 我只改了第 5 行，diff 里看起来整个文件只变了第 5 行，可第 80 行其实早被
    # 别的进程重写了，而这条信息在输出里一个字都没有。之后 sha1 一刷，
    # 那处改动就永久并进了「基准」，再也不会被提及。
    diff_base: list[str] | None = None
    if external_changed:
        old_shadow, _why = _load_shadow(path, meta)
        if old_shadow:
            diff_base = old_shadow

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
    diff = unified(diff_base if diff_base is not None else old_lines, new_lines,
                   args.label or path.name)
    rng = changed_lines_range(old_lines, new_lines) or hunks_new_range(diff)

    disk = sha1_of(path)
    meta.update({
        "path": str(path.resolve()), "sha1": disk, "lines": len(new_lines),
        "encoding": enc, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
        "version": meta.get("version", 0) + 1,
        "last_edit_at": time.time(), "last_edit_range": list(rng) if rng else None,
        "last_diff": diff[:200_000], "reason": args.reason or "",
        # 内容变了、行数也可能变了 —— 旧版本的「已读全文」凭证必须作废。
        # 以前这里不碰这两项：改动之后 sha1 换成新值、version 加一，但 full_version
        # 还停在上一个版本号，read_ranges 也照旧，于是下一次 read 精确命中 cached：
        # 「内容未变，区间 ['1..10']」—— 而新内容一个字都没交付过。
        "full_version": None,
        "read_ranges": [],
    })
    if path.stat().st_size <= SHADOW_LIMIT:
        meta["shadow"] = save_shadow(path, new_lines)
        if not meta["shadow"]:
            meta["shadow_error"] = _LAST_SHADOW_ERROR
    _save_meta(path, meta)

    print(f"# 已改 {path}  → v{meta['version']}  {len(new_lines)} 行"
          + (f"  区间 {rng[0]}..{rng[1]}" if rng else ""))
    if external_changed:
        # 必须说出来。否则调用方以为「我读到的 + 我改的 = 磁盘现状」，
        # 而中间那段别人的改动从来没进过任何人的上下文。
        if diff_base is not None:
            print("# ⚠ 注意：我上次读完之后，这个文件被别处改过（磁盘 sha1 变了）。"
                  "下面的 diff 是「上次读到的版本 → 现在」，所以里面包含别人的改动。")
        else:
            print("# ⚠ 注意：我上次读完之后，这个文件被别处改过（磁盘 sha1 变了），"
                  "而且缓存里的旧版本快照不可用 —— 下面的 diff 只覆盖我这次改的部分，"
                  "别人的改动不在里面。要看完整现状就用 read --mode full。")
    if args.reason:
        print(f"# reason: {args.reason}")
    # edit 的 diff 以前是裸 print(diff)：一次 --all 大改能刷出几十 KB，把上下文吃掉，
    # 而 write 早就有 --max-chars 兜着。同一个技能里两套规矩，最常用的那条偏偏没上限。
    sp = _emit(diff, args.max_chars, "diff")
    if sp:
        print(f"\n[注意：这次 diff 没完整进入上下文，被截掉的部分只在 {sp}。"
              f"要完整差异用 diff {args.path}；要看文件现状就用 read（会重新给正文，"
              f"不会拿「内容未变」打发你）。]")
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
        elif not (args.encoding or args.force):
            # 原文件读不成文本（二进制 / 超 8MB / 编码不认识）。以前这里只打一句
            # 「将按新文件写」，然后 rc=0 把整个文件换成 UTF-8 —— 一个 UTF-16 的
            # 配置文件就这么被静默转成了另一种编码，签个字都不用。覆盖一个读不懂的
            # 文件必须是显式动作。
            print(f"ERROR: 原文件无法按文本读取（{why}），拒绝直接覆盖: {path}")
            print(f"  确认要整体替换它就加 --force（新内容会按 --encoding 或 UTF-8 写入）。")
            print(f"  只是想改其中一段？那用 edit，或先确认它到底是不是文本。")
            return 2

    # 「来源给了、但内容为空」曾经是一条无声的毁文件通道：`write g.txt --stdin < /dev/null`
    # 把 2 行重要内容清成 0 字节、rc=0、还正常打了 diff —— 无人值守时漏一根管道就中招。
    # 空内容只能由「原来就是空的」或显式 --allow-empty 允许。
    # 判据是「原文件有没有字节」，不是 `strip()`：一个只有三行空白的文件被清成 0 字节，
    # 在 `strip()` 看来「本来就没内容」，可那 3 个字节是用户在磁盘上的东西。
    if not body and not args.allow_empty and existed and original is not None and original.text:
        print(f"ERROR: 新内容是空的（0 字节），而 {path.name} 现在有 {len(old_lines)} 行（{len(original.text)} 字符）。")
        print("  文件没被改动。确实要清空请加 --allow-empty。"
              "（如果是管道/临时文件没拿到内容，先检查来源。）")
        return 2

    # ---- 写入规格：codec / BOM / 换行 ----
    # 原则：**调用方没说的，就保持原文件**。这一条以前被三处默认值破坏了：
    # `--encoding utf-8` 会把原文件的 BOM 丢掉、无末尾换行会被凭空补上、
    # 混合换行会被整篇冲成单一风格 —— 三种都 rc=0 且没有任何提示。
    explicit_bom: bytes | None = None
    if args.encoding:
        want = args.encoding.strip()
        if want.lower() in ("utf8", "utf-8"):
            codec = "utf-8"
        elif want.lower() in ("utf-8-sig", "utf8-sig"):
            codec, explicit_bom = "utf-8", codecs.BOM_UTF8
        else:
            codec = want
            # 宽编码的 BOM 是自描述必需品：utf-16-le 不写 fffe 就成了一份别人读不出来的
            # 文件（连它自己的 read 都会判成 binary）。给了宽编码就自动补 BOM。
            if want.lower() == "utf-16-le":
                explicit_bom = codecs.BOM_UTF16_LE
            elif want.lower() == "utf-16-be":
                explicit_bom = codecs.BOM_UTF16_BE
            elif want.lower() == "utf-32-le":
                explicit_bom = codecs.BOM_UTF32_LE
            elif want.lower() == "utf-32-be":
                explicit_bom = codecs.BOM_UTF32_BE
        try:
            "".encode(codec)
        except LookupError:
            print(f"ERROR: 不认识的编码名: {args.encoding}")
            print("  可用示例: utf-8 / utf-8-sig / gbk / utf-16-le / utf-16-be / latin-1")
            return 2
        # 没显式要求 BOM 时，沿用原文件有没有 BOM —— 换编码不等于要求去掉 BOM。
        bom = explicit_bom if explicit_bom is not None else (original.bom if original else b"")
        nl = original.newline if original else ""
    elif original is not None:
        codec, bom, nl = original.encoding, original.bom, original.newline
    else:
        codec, bom, nl = "utf-8", b"", ""

    # 换行：调用方给的多半是 LF，但目标文件可能是 CRLF。
    # 只有当正文与原文**逐字相同**时才原样写回（那是「没改」，不该被顺手重排）；
    # 否则按目标风格归一 —— 除非原文件本身是混合换行，这时候「归一」就是破坏，
    # 而「不归一」又不知道往哪边靠，于是保持调用方给的字节并在下面明说。
    mixed = bool(original and original.mixed_newline)
    if original is not None and body == original.text:
        target_nl = original.newline
    elif nl:
        target_nl = "\r\n" if nl == "\r\n" else ("\r" if nl == "\r" else "\n")
        if mixed:
            target_nl = ""
        else:
            body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", target_nl)
    else:
        target_nl = "\n"
    # 末尾换行只在**原文件本来就有**（或这是新文件）时才补。以前的
    # `if body and not body.endswith(("\n","\r")): body += target_nl` 会给出一个
    # 原文没末尾换行的文件凭空加一个 —— git diff 里看起来像改了两行，实际只改一行。
    if original is not None:
        original_had_eol = original.text.endswith(("\n", "\r"))
    else:
        # 新文件：**不要**凭空补末尾换行。以前这里写死 True，于是
        # `write n.txt --content-file b.txt`（b.txt 是 `abc` 没有换行）会写出
        # `abc\n` —— 调用方给的是 3 个字节，落盘成了 4 个，而且 rc=0 不说。
        # 「新文件默认该有末尾换行」是一种风格偏好，不是事实；
        # 调用方没说的，就保持调用方给的。
        original_had_eol = body.endswith(("\n", "\r"))
    if original_had_eol and target_nl and body and not body.endswith(("\n", "\r")):
        body += target_nl

    enc = codec + (" BOM" if bom else "") + (" CRLF" if target_nl == "\r\n" else "")
    # 父目录不存在时**默认不替你造目录树**：`write sr/foo.ts` 这种把 src 打成 sr 的
    # 手误，以前会默默 mkdir -p 出一棵新树、写出一个谁也不会读的文件、再报「已新建」，
    # 而真正想改的那个文件一字节没动 —— 失败被自己的成功文案盖住了。
    # 但「新建 src/new/dir/x.ts」是合理需求，所以留 --mkdir 显式授权。
    if not path.parent.is_dir():
        if not args.mkdir:
            print(f"ERROR: 父目录不存在: {path.parent}")
            print("  没有替你创建目录。先确认路径没打错；确实要新建这层目录就加 --mkdir。")
            return 2
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"ERROR: 建不出目录 {path.parent}（{exc.strerror or exc}）。没写任何东西。")
            return 2
    try:
        payload = bom + body.encode(codec)
    except UnicodeEncodeError as exc:
        print(f"ERROR: 内容无法用 {codec} 编码：{exc}")
        print("  文件没被改动。换编码请加 --encoding utf-8")
        return 2
    # 内容与磁盘上逐字节相同时，就是「没改」。以前照写不误：版本号 +1、
    # 缓存凭证作废、还打一行「已改 → v2」—— 一次什么都没改的调用却让下一个
    # read 多给一遍全文，而调用方以为自己在做幂等操作。
    if existed:
        try:
            if path.read_bytes() == payload:
                _prev = _load_meta(path)
                print(f"# 没变化：{path.name} 的字节和你给的内容完全一致，"
                      f"原样未动（缓存版本未变 v{_prev.get('version') or 0}）")
                return 0
        except OSError:
            pass
    try:
        _atomic_write(path, payload)
    except OSError as exc:
        # 只读文件 / 目录不可写 / 被别的进程占着 —— 说清楚是哪个环节失败，
        # 别让用户对着一屏 Traceback 猜。
        print(f"ERROR: 写不进 {path}（{exc.strerror or exc}）")
        print("  文件没被改动。检查权限、或文件是否被别的程序占用。")
        return 2

    # ---- 写回自检 ----
    # 以前的检查是 `check.text != body` —— 但 check.text 已经把 BOM 剥掉、换行也归一过了，
    # 所以它验证的只是「我自己的解码器和我自己编出来的字节自洽」，根本没有验证
    # 「我有没有保住原文件的样子」。BOM 被丢、末尾换行被补、混合换行被冲平这三种破坏
    # 发生时，它每次都全绿、rc=0，还照样打印「已改」。**这是比单个 bug 更严重的东西：
    # 它给人的是一条虚假的安全感。**
    # 现在直接比字节：磁盘上的必须就是我写下去的那串字节，一个字都不能差。
    try:
        on_disk = path.read_bytes()
    except OSError as exc:
        print(f"ERROR: 写完读不回来（{exc.strerror or exc}）: {path}")
        return 2
    if on_disk != payload:
        print(f"# ⚠ 写回自检未通过：磁盘上的字节和写下去的不一致（{len(on_disk)} vs {len(payload)} 字节）")
        print(f"#   文件可能被别的程序同时改动。请重新确认 {path} 的内容。")
        return 2
    check, why2 = load(path)
    if check is None:
        print(f"# ⚠ 写回自检：这份文件写入后自己读不回来（{why2}）。")
        print(f"#   规格 [{enc}] 与内容不兼容，换编码请用 --encoding utf-8。")
        return 2
    if mixed and body != (original.text if original else ""):
        print(f"# 注意：原文件是混合换行，新内容按你给的换行原样写入（没有替你统一）。")

    new_lines = split_lines(body)
    meta = _load_meta(path)
    diff = unified(old_lines, new_lines, args.label or path.name) if old_lines else ""
    rng = (changed_lines_range(old_lines, new_lines) or hunks_new_range(diff)) if old_lines else (1, len(new_lines))
    meta.update({
        "path": str(path.resolve()), "sha1": sha1_of(path), "lines": len(new_lines),
        "encoding": enc, "size": path.stat().st_size, "mtime": path.stat().st_mtime,
        # 文件已经不在时（先删再写）版本号从 1 重新起算：`# 已新建 … → v2` 自相矛盾，
        # 而这个 v2 是上一份文件留下的，对现在这份没有任何连续性可言（old_lines 也是空的）。
        "version": 1 if not existed else (meta.get("version") or 0) + 1,
        "last_edit_at": time.time(), "last_edit_range": list(rng) if rng else None,
        "last_diff": diff[:200_000], "reason": args.reason or "",
        # 内容变了、行数也可能变了 —— 旧版本的「已读全文」凭证必须作废。
        # 以前这里不碰这两项：改动之后 sha1 换成新值、version 加一，但 full_version
        # 还停在上一个版本号，read_ranges 也照旧，于是下一次 read 精确命中 cached：
        # 「内容未变，区间 ['1..10']」—— 而新内容一个字都没交付过。
        "full_version": None,
        "read_ranges": [],
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
                  f"要完整差异就看那个文件或用 diff {args.path}；"
                  f"要看文件现状就用 read（会重新给正文）。]")
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
    missing: list[str] = []
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
        else:
            # 以前这种路径既不进 targets 也不进 skipped，就这么消失了，最后只报一句
            # 「没有可摘要的文件（目录里没有常见源码后缀？）」—— 明明是自己拼错了文件名。
            missing.append(raw)
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
        if missing:
            print(f"ERROR: 这些路径不存在: {'、'.join(missing)}")
            print("  拼错了文件名不会得到「没有可摘要的文件」这种误导性提示——这里直接说哪个不存在。")
        else:
            # `--max-files 0` 把 uniq 切空，报的却是「目录里没有常见源码后缀」——
            # 又是在解释一个没发生过的事。先分清「本来就没有」和「被我切掉了」。
            if total_avail:
                print(f"ERROR: --max-files {args.max_files} 把 {total_avail} 个候选文件全截掉了。")
                print("  目录里确实有可摘要的文件，是上限设成了 0/太小。调大它再试。")
            else:
                print("ERROR: 没有可摘要的文件（目录里没有常见源码后缀？可用 --max-files 放宽）")
        return 2
    if missing:
        print(f"# ⚠ 跳过不存在的路径 {len(missing)} 个: {'、'.join(missing)}")

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
    if path.is_dir():
        # read/edit/write 都会区分目录，state/diff 以前不区分，只说「无缓存」——
        # 而写错路径时那句话会把人引向「缓存坏了」这个完全错误的方向。
        print(f"ERROR: 这是目录不是文件: {path}")
        print(f"  想看目录里有几个文件用了缓存: fstate.py digest {path}")
        return 2
    meta = _load_meta(path)
    if not meta:
        extra = f"（原缓存条目已损坏并丢弃：{_LAST_META_BAD}）" if _LAST_META_BAD else ""
        print(f"{path}: 无缓存（还没用 fstate read 过）{extra}")
        return 0
    disk = sha1_of(path) if path.is_file() else None
    if disk is None:
        # 文件没了和文件改了是两回事：前者让你去 --mode full 也读不出来。
        # 以前这两种都回「STALE（磁盘已变，需要 --mode full 重新加载）」，
        # 照着提示做只会得到「文件不存在」，白跑一趟。
        stale = "MISSING（文件已不存在；缓存记录还在，文件恢复后会自动比对新旧）"
    elif disk != meta.get("sha1"):
        stale = "STALE（磁盘已变，需要 --mode full 重新加载）"
    else:
        stale = "fresh"
    # 区间按当前行数裁剪，口径与 `_bump`/`read` 保持一致：
    # 文件被改短后旧区间会越界，直接照抄「1..999」是凭空造出来的行号。
    _n = meta.get("lines") or 0
    _rr: list[list[int]] = []
    for _r in (meta.get("read_ranges") or []):
        if isinstance(_r, list) and len(_r) == 2:
            _a, _b = max(1, int(_r[0])), min(_n, int(_r[1])) if _n else int(_r[1])
            if _a <= _b:
                _rr.append([_a, _b])
    print(json.dumps({
        "path": meta.get("path"),
        "version": meta.get("version"),
        "lines": meta.get("lines"),
        "read_count": meta.get("read_count"),
        "read_ranges": _rr,
        # 原始区间（未裁剪）。两者不同就说明文件被改短过，行号已经不能直接对上了。
        "read_ranges_raw": meta.get("read_ranges"),
        # 「读过全文」的凭证是否还有效（版本没变 + 同一会话）。
        # 以前 state 只有 fresh/STALE/MISSING 三态，把「只读过第 5..10 行」的
        # 文件也报成 fresh —— 看起来像「这个文件我已经掌握了」。
        "full_read_version": meta.get("full_version"),
        "full_read_valid": (
            meta.get("full_version") is not None
            and meta.get("full_version") == meta.get("version")
            and bool(_session_id())
            and meta.get("session") == _session_id()
        ),
        "session": meta.get("session"),
        "last_edit_range": meta.get("last_edit_range"),
        "last_read_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("last_read_at", 0)))
        if meta.get("last_read_at") else None,
        "cache_state": stale,
    }, ensure_ascii=False, indent=1))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    p = resolve_input(args.path)
    if p.is_dir():
        print(f"ERROR: 这是目录不是文件: {p}")
        return 2
    meta = _load_meta(p)
    d = meta.get("last_diff")
    if not d:
        print("没有记录到 diff（还没用本脚本编辑过该文件）")
        return 0
    _emit(d, args.max_chars, "diff")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    path = resolve_input(args.path)
    removed = 0
    for p in (_meta_path(path), _shadow_path(path)):
        if p.is_file():
            p.unlink()
            removed += 1
    if removed:
        print(f"已清缓存: {path}")
    else:
        # 以前不管有没有都回「已清缓存」，于是「我明明清过了」变成一个无法证伪的说法。
        print(f"没有缓存可清: {path}（本来就没 read/edit 过）")
    return 0


def cmd_hot(args: argparse.Namespace) -> int:
    rows = []
    orphan_shadows = 0
    if CACHE_HOME.is_dir():
        known = {f.stem for f in CACHE_HOME.glob("*.json")}
        # 影子副本没有对应的 .json 时永远不会有命令去碰它 ——
        # reset 是按路径删的，json 都没了就没人记得这个 shadow 属于谁。
        # 不数出来，这些文件会安静地占着磁盘直到手动翻目录。
        orphan_shadows = sum(1 for f in CACHE_HOME.glob("*.shadow") if f.stem not in known)
        for f in CACHE_HOME.glob("*.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            # 结构不对的条目在这里也要筛掉：以前 `[]` 或错类型会一路带到
            # rows.append 里变成 `'list' object has no attribute 'get'` 裸栈。
            if not _meta_sane(m)[0]:
                continue
            rows.append((m.get("read_count", 0), m.get("path", "?"), m.get("lines", 0)))
    rows.sort(reverse=True)

    def _spill_line() -> str:
        """溢出文件（被 --max-chars 截断时落盘的全文）只增不减：
        reset 按路径删、只动 .json 与 .shadow，碰不到 spill/ 目录。
        不数出来，这里会一直涨，而且「我明明 reset 过了」还清不掉。"""
        spill_dir = CACHE_HOME / "spill"
        if not spill_dir.is_dir():
            return ""
        n = 0
        size = 0
        for f in spill_dir.iterdir():
            if f.is_file():
                n += 1
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
        if not n:
            return ""
        return (f"#  共 {n} 个溢出文件（被截断的全文落盘），占 {size / 1024:.0f} KB：{spill_dir}\n"
                f"   只有补看被截掉的内容时才用得上；reset 不会清这里，要收回空间就手动删。")

    if not rows:
        print("缓存里还没有记录（还没用 fstate read 过任何文件）")
        if orphan_shadows:
            print(f"# ⚠ 但有 {orphan_shadows} 个孤儿影子副本（没有对应 .json），占着磁盘")
        tail = _spill_line()
        if tail:
            print(tail)
        return 0
    print("读次数  行数   文件")
    for c, p, n in rows[: args.n]:
        flag = "  ← 反复读，考虑改用 range/diff" if c >= 5 else ""
        print(f"{c:6d}  {n:5d}   {p}{flag}")
    if len(rows) > args.n:
        print(f"（共 {len(rows)} 个文件有缓存记录，只显示前 {args.n} 个；看全部加 --n {len(rows)}）")
    if orphan_shadows:
        print(f"# ⚠ 另有 {orphan_shadows} 个孤儿影子副本（对应的 .json 已不在），"
              f"没有任何命令会清理它们；要收回空间就手动删 {CACHE_HOME}")
    # 溢出文件（被 --max-chars 截断时落盘的全文）同样是只增不减的：
    # reset 按路径删、只动 .json 与 .shadow，碰不到 spill/ 目录。
    # 不数出来，这里会一直涨，而且「我明明 reset 过了」还清不掉。
    tail = _spill_line()
    if tail:
        print(tail)
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
    unreadable: list[tuple[str, str]] = []
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
        text, why_read = read_text(p)
        if text is None:
            # 单个文件读不了（被独占、没权限、二进制）不该让整次摸底失败。
            # explore 恰恰是拿去「在陌生仓库里翻东西」的，一个被锁住的 .db/.log
            # 以前会让整个命令吐 Traceback，连已经命中的文件都看不到。
            if why_read and why_read not in ("binary", "toolarge"):
                unreadable.append((rel, why_read))
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
                     "defline": def_line(lines, terms), "nlines": len(lines),
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
    if skipped_bundle:
        # 以前这个计数器只加不读：打包产物被静默跳过，没人知道。
        # 而「答案没找到」和「答案在那个 bundle 里但我没看」是完全不同的两件事。
        print(f"  ⚠ 另有 {skipped_bundle} 个文件因含超长行（打包/压缩产物）被跳过，没参与匹配。")
    if unreadable:
        names = "、".join(f"{r}（{w}）" for r, w in unreadable[:5])
        more = f" 等 {len(unreadable)} 个" if len(unreadable) > 5 else ""
        print(f"  ⚠ 跳过 {len(unreadable)} 个读不了的文件: {names}{more}")
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
        # 建议的命令必须能直接跑通：
        # ① 用绝对路径。rel 是相对 --root 的，当 root≠cwd 时照抄会得到「文件不存在」。
        # ② --end 不能越过文件实际行数（1 行文件曾建议 --end 41）。
        nl = h0.get("nlines") or 0
        start = max(1, first - 5)
        end = first + 40
        if nl:
            end = min(end, nl)
        if end < start:
            end = start
        print(f"下一步: fstate.py read \"{h0['abs']}\" --mode range --start {start} --end {end}")
        print(f"        fstate.py edit \"{h0['abs']}\" --old ... --new ...   （回 diff，不必重读全文）")
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

    e = sub.add_parser("explore", allow_abbrev=False, help="一次成型探索：目录概览 + 命中 + 签名 + 下一步")
    e.add_argument("query", help="要查的关键词（标识符、函数名、报错片段）")
    e.add_argument("--root", default=".", help="搜索根目录，默认当前目录")
    e.add_argument("--max-files", type=_positive_int, default=3, help="详细列出几个命中文件（默认 3）")
    e.add_argument("--max-hits", type=_positive_int, default=3, help="每文件列几处命中行（默认 3）")
    e.add_argument("--max-sigs", type=_positive_int, default=6, help="每文件列几条签名（默认 6）")
    e.add_argument("--snippet-lines", type=_positive_int, default=4, help="每处命中带几行上下文（默认 4）")
    e.add_argument("--max-scan", type=_positive_int, default=6000, help="最多扫描多少个文件（默认 6000，超了会明说截断）")
    e.add_argument("--min-score", type=float, default=3.0, help="低于这个分数的命中不列（默认 3.0）")
    e.add_argument("--max-chars", type=_positive_int, default=24000, help="输出字符上限，超了落盘并只给预览（默认 24000）")
    e.set_defaults(fn=cmd_explore)

    r = sub.add_parser("read", allow_abbrev=False, help="带缓存的读：未变→摘要；变了→diff；范围→行号区间")
    r.add_argument("path", help="要读的文件")
    r.add_argument("--mode", default="auto",
                   choices=["auto", "full", "range", "diff", "changed", "cached"],
                   help="auto=自己决定（默认）；full=整篇；range=行区间；"
                        "changed=只要与上次的差异（没变就明说没变）；diff=同上但不会自动降级为全文；"
                        "cached=只问状态、绝不输出正文")
    # 行号是 1-based。以前 --start 0 会被 max(1, ...) 悄悄改成第 1 行，于是
    # 「--start 0 --end 2」静默等价于「1..2」—— 调用方以为自己在读第 0 行往后的
    # 东西，实际偏移了一行还浑然不觉。宁可报错，不猜。
    r.add_argument("--start", type=_positive_line, help="起始行号（1-based，含）")
    r.add_argument("--end", type=_positive_line, help="结束行号（含）")
    # 默认 None 而不是 400：默认值只该管「auto 落下来的全文读」，
    # 显式 --mode full 说的是「我就要整篇」，不该被默认值推翻。
    r.add_argument("--max-lines", type=_positive_int, default=None,
                   help=f"最多输出几行（不给时：--mode full 给整篇，其余默认 {DEFAULT_MAX_LINES} 行）")
    r.add_argument("--max-chars", type=_positive_int, default=20000, help="输出字符上限，超了落盘并只给预览（默认 20000）")
    r.add_argument("--label", default="", help="在 header 与 diff 的 a/ b/ 前缀里显示这个名字")
    r.set_defaults(fn=cmd_read)

    w = sub.add_parser("edit", allow_abbrev=False, help="编辑并回 unified diff + 变更行区间")
    w.add_argument("path", help="要改的文件")
    w.add_argument("--old", default="", help="要替换掉的原文片段（必须唯一，或用 --all）")
    # default=None 而不是 ""：默认空串的话，调用方漏给 --new 时会被当成
    # 「把 old 替换成空」——明明是忘了参数，结果内容被静默删掉，diff 里还显示
    # 「已改」。设成 None 才能区分「没给」和「显式给了空串（=删除）」。
    w.add_argument("--new", default=None, help="替换成什么；删除请显式传 --new ''（漏给会报错）")
    w.add_argument("--old-file", default="", help="从文件读 --old（片段较长时用，免于 shell 转义）")
    w.add_argument("--new-file", default="", help="从文件读 --new")
    w.add_argument("--all", action="store_true", help="替换全部出现处，否则要求 old 唯一")
    w.add_argument("--reason", default="", help="记一句为什么要改（写进缓存，便于回看）")
    w.add_argument("--label", default="", help="diff 的 a/ b/ 前缀显示名")
    w.add_argument("--max-chars", type=_positive_int, default=12000, help="diff 输出字符上限，超了落盘并只给预览（默认 12000）")
    w.set_defaults(fn=cmd_edit)

    wr = sub.add_parser("write", allow_abbrev=False, help="新建/整体覆盖并回 diff")
    wr.add_argument("path", help="目标文件（已存在则整体覆盖）")
    wr.add_argument("--content-file", default="", help="从文件读新内容（UTF-8/GBK 均可）")
    wr.add_argument("--stdin", action="store_true", help="从标准输入读新内容")
    wr.add_argument("--encoding", default="",
                    help="写入编码；不给则沿用原文件（新文件用 UTF-8）。"
                         "utf-8-sig 带 BOM；utf-16-le 等宽编码会自动补 BOM")
    wr.add_argument("--force", action="store_true",
                    help="原文件读不成文本（二进制/超 8MB）时也强行覆盖 —— 默认拒绝")
    wr.add_argument("--mkdir", action="store_true",
                    help="父目录不存在时替你建出来 —— 默认报错（手误写错目录不该静默成功）")
    wr.add_argument("--allow-empty", action="store_true",
                    help="允许把非空文件写成空内容（默认拒绝：空输入多半是管道/临时文件没拿到内容）")
    wr.add_argument("--reason", default="", help="记一句为什么要改（写进缓存）")
    wr.add_argument("--label", default="", help="diff 的 a/ b/ 前缀显示名")
    wr.add_argument("--max-chars", type=_positive_int, default=12000, help="diff 输出字符上限（默认 12000）")
    wr.set_defaults(fn=cmd_write)

    s = sub.add_parser("state", allow_abbrev=False, help="文件状态（版本/行数/已读区间/凭证是否有效）")
    s.add_argument("path", help="要查的文件")
    s.set_defaults(fn=cmd_state)

    dg = sub.add_parser("digest", allow_abbrev=False, help="多文件结构摘要：行数 + 顶层签名 + 缓存状态")
    dg.add_argument("paths", nargs="+", help="文件或目录（可多个）")
    dg.add_argument("--max-files", type=_positive_int, default=20, help="最多摘要几个文件（默认 20）")
    dg.add_argument("--max-sigs", type=_positive_int, default=12, help="每文件列几条签名（默认 12）")
    dg.add_argument("--max-scan", type=_positive_int, default=400, help="目录最多扫描几个文件（默认 400）")
    dg.add_argument("--all-ext", action="store_true", help="连非源码后缀（.md/.txt/无后缀）也摘要")
    dg.set_defaults(fn=cmd_digest)

    d = sub.add_parser("diff", allow_abbrev=False, help="回放最近一次 diff（不受上次预览截断影响）")
    d.add_argument("path", help="目标文件")
    d.add_argument("--max-chars", type=_positive_int, default=12000, help="输出字符上限（默认 12000）")
    d.set_defaults(fn=cmd_diff)

    rs = sub.add_parser("reset", allow_abbrev=False, help="清掉某个文件的缓存")
    rs.add_argument("path", help="要清缓存的文件")
    rs.set_defaults(fn=cmd_reset)

    h = sub.add_parser("hot", allow_abbrev=False, help="读得最多的文件排行（含孤儿影子副本提示）")
    h.add_argument("--n", type=_positive_int, default=15, help="列前几个（默认 15）")
    h.set_defaults(fn=cmd_hot)

    args = ap.parse_args()
    # 最后一道兜底：读侧（sha1_of / load / read_bytes）以前完全没有防御，
    # 而它们位于业务流程最前面 —— 于是「文件被 Excel 独占」「没权限读」
    # 这两种最常见的真实场景会直接吐一屏 Traceback 加 rc=1，
    # 而下游那些写得很好的「文件没被改动」分支一次都轮不到执行。
    try:
        return args.fn(args)
    except OSError as exc:
        where = getattr(args, "path", "")
        name = exc.strerror or str(exc)
        if str(exc).startswith("没有可读的 stdin"):
            # stdin 被关掉不是「文件被占用」，别把两条完全不同的建议混在一起。
            print(f"ERROR: {exc}。")
            print("  要么用 --content-file <文件>，要么把内容管道过来。文件没被改动。")
            return 2
        print(f"ERROR: 读写失败（{name}）: {where}")
        print("  文件可能正被别的程序占用（Word/Excel/编辑器/杀软），或没有权限。"
              "关掉它再试；文件本身没有被改过。")
        return 2
    except KeyboardInterrupt:
        print("\n已中断（没有写盘，或写盘是原子的：不会留下半截文件）。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
