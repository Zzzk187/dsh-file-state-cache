# tests/test_hardening_continue.py
"""「继续」轮增量回归：收掉审计残留的轻量真 bug，并做实 POSIX 分支复核。

本机没有 Linux 内核（WSL 无发行版 / 无 Docker），真机实跑做不到；这里用
「注入假 fcntl + 代理 os.name」的白盒测试固化 _cache_lock 在 POSIX 分支下的
加锁/释放配对，作为跨平台复核中能在本机完成的部分，其余如实标未验证。

本文件前 3 组（A/B/C）是「先写测试复现 bug」：修复前应为 FAIL；
D 组是对既有 POSIX 分支的固化复核，基线即 PASS，用于防止回归。
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------- A: --min-score 有限性
def test_min_score_nan_is_rejected(fs):
    # 旧版 --min-score nan 让 `score < nan` 恒为 False，等于没有门槛 -> 命中全列（灌水）。
    fs.write("a.py", "alpha alpha alpha\n")
    r = fs.run("explore", "alpha", "--min-score", "nan")
    assert r.rc == 2, r.text
    assert "min-score" in r.text or "有限" in r.text or "分数" in r.text


def test_min_score_inf_is_rejected(fs):
    fs.write("a.py", "alpha alpha alpha\n")
    r = fs.run("explore", "alpha", "--min-score", "inf")
    assert r.rc == 2, r.text


def test_min_score_normal_value_still_works(fs):
    fs.write("a.py", "alpha alpha alpha\n")
    r = fs.run("explore", "alpha", "--min-score", "1")
    assert r.rc == 0, r.text
    assert "命中" in r.text


# ---------------------------------------------------------------- B: stdin 非法字节
def test_illegal_stdin_bytes_are_rejected_not_silently_replaced(fs):
    # b"\x80" 在 utf-8 与 gbk 都解不出来（已探针确认；注意 b"\x81w" 是合法 GBK
    # 双字节，会被解成「亀」，不能拿来当非法字节）。旧实现会 errors="replace"
    # 把它换成 U+FFFD 写进文件——静默损坏用户内容。
    r = fs.run("write", "out.txt", "--stdin", stdin=b"hello\x80world")
    assert r.rc == 2, r.text
    assert not fs.exists("out.txt"), "一个字节都不该写进去"
    assert "可识别" in r.text or "非法字节" in r.text or "解不出" in r.text


def test_legal_utf8_stdin_still_writes(fs):
    r = fs.run("write", "out.txt", "--stdin", stdin="hello\nworld\n")
    assert r.rc == 0, r.text
    assert fs.read_bytes("out.txt") == b"hello\nworld\n"


def test_legal_gbk_stdin_still_writes(fs):
    # GBK stdin 被解成 str 后、新文件按 utf-8 写出（既有设计：--stdin 给的是文本，
    # 不是「按原字节透传」）。所以断言文本内容，而不是逐字节等于 GBK 输入。
    payload = "中文内容\n".encode("gbk")
    r = fs.run("write", "out.txt", "--stdin", stdin=payload)
    assert r.rc == 0, r.text
    assert fs.read_bytes("out.txt").decode("utf-8") == "中文内容\n"


# ---------------------------------------------------------------- C: 剪枝可观测
def test_explore_reports_pruned_dirs_and_files(fs):
    fs.write("code.py", "needle needle needle needle\n")
    fs.write_bytes("image.png", b"\x89PNG\x00\x01\x02binary")
    (fs.work / "node_modules").mkdir()
    fs.write("node_modules/lib.py", "needle\n")
    r = fs.run("explore", "needle")
    assert r.rc == 0, r.text
    assert "剪枝" in r.text, "跳过必须让用户看得见"


def test_digest_reports_pruned_dirs(fs):
    (fs.work / "build").mkdir()
    fs.write("build/x.py", "def foo():\n    pass\n")
    fs.write("a.py", "def bar():\n    pass\n")
    r = fs.run("digest", ".")
    assert r.rc == 0, r.text
    assert "剪枝" in r.text


def test_no_prune_no_extra_line(fs):
    # 没发生剪枝时不加噪音行（保持 CLI 输出兼容）。
    fs.write("a.py", "needle\n")
    r = fs.run("explore", "needle")
    assert r.rc == 0
    assert "剪枝" not in r.text


# ---------------------------------------------------------------- D: POSIX 锁分支复核
class _FakeFcntl:
    LOCK_EX = 2
    LOCK_UN = 8

    def __init__(self, calls):
        self.calls = calls

    def flock(self, fd, op):
        self.calls.append(op)


class _OsProxy:
    """只把 name 伪装成 posix，其余属性转发给真 os（避免污染全局 os.name）。"""

    name = "posix"

    def __init__(self, real):
        self._real = real

    def __getattr__(self, k):
        return getattr(self._real, k)


def _arm_posix(mod, monkeypatch):
    calls = []
    fake = _FakeFcntl(calls)
    monkeypatch.setattr(mod, "fcntl", fake, raising=False)
    monkeypatch.setattr(mod, "os", _OsProxy(os), raising=False)
    return calls, fake


def test_posix_lock_branch_flock_ex_then_un(mod, monkeypatch):
    calls, fake = _arm_posix(mod, monkeypatch)
    p = mod.CACHE_HOME.parent / "work" / "t.txt"
    with mod._cache_lock(p):
        assert fake.LOCK_EX in calls, "进入临界区前应 flock(LOCK_EX)"
    assert calls, "应发生过 flock 调用"
    assert calls[-1] == fake.LOCK_UN, "退出临界区应 flock(LOCK_UN)"


def test_posix_lock_released_even_on_exception(mod, monkeypatch):
    calls, fake = _arm_posix(mod, monkeypatch)
    p = mod.CACHE_HOME.parent / "work" / "t.txt"
    with pytest.raises(RuntimeError):
        with mod._cache_lock(p):
            raise RuntimeError("boom")
    assert calls[-1] == fake.LOCK_UN, "异常时 finally 仍要解锁"
