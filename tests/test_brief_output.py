# tests/test_brief_output.py
"""1.1.1 L1 精简档（--brief）回归测试。

背景（perf-eval/REPORT.md 第 7/9 节）：在「一次性 explore+read」和「digest 大目录」
两个场景里，fstate 的输出比原生文件操作反而多花 11~114% 的 token。冗余主要来自
explore 的目录/后缀概览、签名、片段、edit 建议，以及 digest 每文件多条签名 + 每条
文件重复的「签名未列完」提示。

本轮给 explore/digest 各加一个默认关闭的 `--brief` 开关：
  * 不加开关时输出逐字不变（CLI 兼容的硬前提）；
  * 加开关时砍掉冗余，但保留定位信息（命中行、下一步 read、文件标题）和全部 ⚠ 警告；
  * 精简的是「探索/摘要输出」，不是文件正文 —— 绝不能因此产生「已读」凭证（P0 红线）。

所有用例都是黑盒 CLI 子进程，和真实调用方式一致。
"""
from __future__ import annotations

import re

import pytest


def _explore_file(tag: str) -> str:
    """一个含 gamma 命中、若干顶层 def、且关键词不是定义名的文件。

    - gamma 出现在多行 -> occ>0、score 够 --min-score 3.0
    - 没有 `def gamma` -> isdef=False -> explore 的 strong=False -> 概览/签名/片段都会出现
    - 有 SIG_RE 能匹配的 def 行 -> sigs 非空
    - 命中行之外还有内容行 -> 片段非空
    """
    return (
        f"# gamma note for {tag}\n"
        f"def helper_{tag}_a():\n"
        f"    val = 'gamma_{tag}_a'\n"
        f"    return val\n"
        f"# gamma again {tag}\n"
        f"data = gamma_{tag}_b\n"
        f"def helper_{tag}_b():\n"
        f"    return 'gamma_{tag}_b'\n"
    )


def _digest_file(tag: str) -> str:
    """4 个顶层 def，配合 --max-sigs 2 让每个文件都触发「签名未列完」。"""
    parts = [f"# module {tag}"]
    for i in range(4):
        parts += [f"def func_{tag}_{i}():", f"    return {i}"]
    return "\n".join(parts) + "\n"


def _sig_lines(out: str):
    """数 digest 输出里的 `行号| ...` 签名/head 行。"""
    return re.findall(r"(?m)^\s*\d+\| ", out)


@pytest.fixture
def explore_world(fs):
    fs.write("a.py", _explore_file("a"))
    fs.write("b.py", _explore_file("b"))
    return fs


@pytest.fixture
def digest_world(fs):
    fs.write("a.py", _digest_file("a"))
    fs.write("b.py", _digest_file("b"))
    fs.write("c.py", _digest_file("c"))
    return fs


# ---------------------------------------------------------------- 复现：默认输出确实臃肿
def test_default_explore_has_all_the_noise(explore_world):
    """默认 explore 输出含目录/后缀概览、签名、片段、edit 建议（这就是冗余来源）。"""
    r = explore_world.run("explore", "gamma", "--root", ".")
    assert r.rc == 0, r.text
    assert "  目录: " in r.out          # 概览
    assert "  后缀: " in r.out          # 概览
    assert "  签名 " in r.out           # 签名行
    assert "  片段 " in r.out           # 片段行
    assert "fstate.py edit" in r.out    # edit 建议
    assert "### " in r.out              # 命中文件标题
    assert "  命中 " in r.out           # 命中行
    assert "下一步: fstate.py read" in r.out


def test_default_digest_repeats_truncation_note(digest_world):
    """默认 digest 每个文件都重复一句「签名未列完」，且列多条签名。"""
    r = digest_world.run("digest", "a.py", "b.py", "c.py", "--max-sigs", "2")
    assert r.rc == 0, r.text
    assert r.out.count("签名未列完") == 3, r.out   # 3 个文件各一次
    assert len(_sig_lines(r.out)) == 6, r.out      # 3 文件 × 2 条签名


# ---------------------------------------------------------------- --brief 更短且不丢必要信息
def test_brief_explore_is_shorter_but_keeps_locators(explore_world):
    default = explore_world.run("explore", "gamma", "--root", ".")
    brief = explore_world.run("explore", "gamma", "--root", ".", "--brief")
    assert default.rc == 0
    assert brief.rc == 0, brief.text
    assert len(brief.out) < len(default.out), (len(brief.out), len(default.out))
    # 砍掉的冗余
    assert "  目录: " not in brief.out
    assert "  后缀: " not in brief.out
    assert "  签名 " not in brief.out
    assert "  片段 " not in brief.out
    assert "fstate.py edit" not in brief.out
    # 保留的定位信息
    assert "### " in brief.out
    assert "  命中 " in brief.out
    assert "下一步: fstate.py read" in brief.out
    assert "# 探索" in brief.out
    # 砍了什么要明说
    assert "精简档" in brief.out and "--brief" in brief.out


def test_brief_explore_keeps_warnings(explore_world):
    """精简档不能把 ⚠ 截断/跳过警告一起砍掉（诚实性护栏）。"""
    # --max-scan 1 且目录里 >1 个文件 -> 触发扫描截断警告
    default = explore_world.run("explore", "gamma", "--root", ".", "--max-scan", "1")
    brief = explore_world.run("explore", "gamma", "--root", ".", "--max-scan", "1", "--brief")
    assert default.rc == 0 and brief.rc == 0
    assert "扫描在" in default.out and "⚠" in default.out
    assert "扫描在" in brief.out and "⚠" in brief.out   # 警告必须保留


def test_brief_digest_one_sig_per_file_no_repeated_note(digest_world):
    default = digest_world.run("digest", "a.py", "b.py", "c.py", "--max-sigs", "2")
    brief = digest_world.run("digest", "a.py", "b.py", "c.py", "--max-sigs", "2", "--brief")
    assert default.rc == 0
    assert brief.rc == 0, brief.text
    assert len(brief.out) < len(default.out), (len(brief.out), len(default.out))
    # 每文件只留首条签名
    assert len(_sig_lines(brief.out)) == 3, brief.out
    # 重复的「签名未列完」合并成一行全局说明
    assert brief.out.count("签名未列完") == 1, brief.out
    # 文件标题与精简说明保留
    assert brief.out.count("## ") == 3
    assert "精简档" in brief.out and "--brief" in brief.out


# ---------------------------------------------------------------- 护栏：不碰缓存正确性
def test_brief_does_not_create_read_credential(digest_world):
    """精简的是探索/摘要输出，不是文件正文。

    explore/digest 命中数恒为 0、从不签发「已完整交付」凭证（perf-eval 第 5 节）。
    跑过 --brief 之后，read --mode full 仍必须给出完整正文，绝不能回「内容未变」。
    """
    fs = digest_world
    assert fs.run("explore", "func_a", "--root", ".", "--brief").rc == 0
    assert fs.run("digest", "a.py", "--brief").rc == 0
    full = fs.run("read", "a.py", "--mode", "full", "--max-chars", "200000")
    assert full.rc == 0, full.text
    assert "内容未变" not in full.out            # 没被误判为「本会话已交付过」
    assert re.search(r"(?m)^\s*\d+\| ", full.out)  # 真的给了带行号正文
    assert "def func_a_0():" in full.out


def test_brief_keeps_error_paths(explore_world, digest_world):
    """--brief 不吞异常：空关键词 / 无候选文件仍按原样报错 rc=2。"""
    empty = explore_world.run("explore", "", "--root", ".", "--brief")
    assert empty.rc == 2 and "关键词不能为空" in empty.text
    miss = digest_world.run("digest", "nope.py", "--brief")
    assert miss.rc == 2 and "nope.py" in miss.text


def test_default_explore_unchanged_by_brief_capability(explore_world):
    """加了 --brief 能力，但默认 explore 输出的关键锚点一个不少一个不多。"""
    ex = explore_world.run("explore", "gamma", "--root", ".")
    assert "精简档" not in ex.out          # 默认不带精简说明
    assert "fstate.py edit" in ex.out      # 默认仍给 edit 建议
    assert "  片段 " in ex.out


def test_default_digest_unchanged_by_brief_capability(digest_world):
    """加了 --brief 能力，但默认 digest 逐文件提示照旧。"""
    dg = digest_world.run("digest", "a.py", "--max-sigs", "2")
    assert "精简档" not in dg.out
    assert dg.out.count("签名未列完") == 1   # 单文件默认也保留逐文件提示


def test_brief_digest_no_op_when_nothing_to_trim(fs):
    """精简档不该在「没东西可精简」时反而变长（真实倒退复现）。

    perf-eval 复测（measure_brief.py）发现：small 目录里每个文件只有 1 条签名，
    --brief 实际什么都没砍，却固定加了一行「精简档」说明，token 从默认的
    -17.9%（比原生省）倒退成 +10.0%（比原生费）。
    修复：说明行只在真的省略了内容（每文件多条签名 / 触发截断）时才打印；
    没砍东西时 --brief 与默认输出逐字一致，不产生任何额外开销。
    """
    fs.write("a.py", "def only_one():\n    return 1\n")
    default = fs.run("digest", "a.py")
    brief = fs.run("digest", "a.py", "--brief")
    assert default.rc == 0 and brief.rc == 0
    assert len(brief.out) <= len(default.out), (len(brief.out), len(default.out))
    # 没省略时也就不该宣称「精简档」（诚实护栏：砍了才明说，没砍别加噪音）
    assert "精简档" not in brief.out


def test_brief_digest_note_appears_only_when_trimmed(digest_world):
    """有省略（多签名/截断）时说明行仍在，砍了什么必须明说。"""
    r = digest_world.run("digest", "a.py", "--max-sigs", "2", "--brief")
    assert r.rc == 0, r.text
    assert "精简档" in r.out          # 4 个 def 被压成 1 条 -> 确实砍了
    assert r.out.count("签名未列完") == 1  # 全局说明 1 行，逐文件提示已合并
