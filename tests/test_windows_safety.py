# tests/test_windows_safety.py
"""Windows 专属：文件锁 / 原子替换 / 异常恢复的真实环境测试。

这些用例只在 Windows 上跑：用 Win32 CreateFileW 造出**真实的跨进程独占句柄**
（杀软/Word/编辑器就是这么占住文件的），再让 fstate 子进程去读写，验证
msvcrt 文件锁、os.replace 重试、临时文件清理、崩溃后不留死锁等行为。

不依赖 pytest-mock，也不改产品代码——全部走真实 CLI 子进程 + 真实句柄。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import FSTATE

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 专属文件安全测试")

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF


@contextmanager
def _win_handle(path: Path, share: int = 0):
    """持有一个真实的 Win32 文件句柄；默认 share=0（完全独占）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = k32.CreateFileW
    CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    CreateFileW.restype = ctypes.c_void_p
    CloseHandle = k32.CloseHandle
    CloseHandle.argtypes = [ctypes.c_void_p]
    CloseHandle.restype = ctypes.c_int32

    h = CreateFileW(str(path), GENERIC_READ | GENERIC_WRITE, share, None,
                    OPEN_EXISTING, 0, None)
    if h is None or h == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(),
                      f"CreateFileW 失败 winerror={ctypes.get_last_error()} path={path}")
    try:
        yield h
    finally:
        CloseHandle(h)


def _tmp_residue(fs) -> list[str]:
    """工作目录 + 缓存目录里所有 fstate 临时文件残留。"""
    return [f.name for d in (fs.work, fs.home) for f in d.iterdir()
            if "fstate-tmp" in f.name]


def _bounded(fs, argv, timeout=15):
    """带超时地跑一条 CLI：死锁会变成 rc=124 而不是把测试挂住。"""
    return fs.run_parallel([argv], timeout=timeout)[0]


# ---------------------------------------------------------------- 并发

def test_win_concurrent_reads_leave_valid_cache_and_shadow(fs):
    """12 个进程同时读同一文件：meta / shadow 都必须是完整可解析的，无临时残留。"""
    p = fs.write("a.txt", "aaa\nbbb\nccc\n")
    results = fs.run_parallel(
        [["read", "a.txt", "--mode", "full"] for _ in range(12)], timeout=40)
    for r in results:
        assert r.rc == 0, r
        assert "[TIMEOUT]" not in r.text, r

    m = fs.meta()                       # 是合法 JSON，没被并发写成半截
    assert m["sha1"] == hashlib.sha1(p.read_bytes()).hexdigest()
    assert m["lines"] == 3
    assert fs.read_shadow().split("\n") == ["aaa", "bbb", "ccc"]
    assert _tmp_residue(fs) == []      # _atomic_cache_write 用完即 replace 掉

    st = fs.run("state", "a.txt")
    assert st.rc == 0
    assert json.loads(st.out)["cache_state"] == "fresh"


def test_win_concurrent_edits_use_lock_and_keep_cache_valid(fs):
    """8 个进程同时 edit：Windows 上应生成 .lock；改动全保留；缓存 sha1 与磁盘一致。"""
    lines = "\n".join(f"token{i}=v{i}" for i in range(8)) + "\n"
    p = fs.write("a.txt", lines)
    argvs = [["edit", "a.txt", "--old", f"token{i}=v{i}", "--new", f"token{i}=V{i}"]
             for i in range(8)]
    results = fs.run_parallel(argvs, timeout=40)
    for r in results:
        assert r.rc == 0, r
        assert "[TIMEOUT]" not in r.text, r

    out = p.read_bytes().decode("utf-8")
    for i in range(8):
        assert f"token{i}=V{i}" in out, out     # 没有 lost update

    assert fs.lock_files(), "Windows 并发 edit 应走 msvcrt 锁，留下 .lock 文件"
    assert fs.meta()["sha1"] == hashlib.sha1(p.read_bytes()).hexdigest()
    assert _tmp_residue(fs) == []


# ---------------------------------------------------------------- 外部修改 / 凭证

def test_win_external_modification_is_detected_never_returns_stale(fs):
    """外部程序直接改字节：下一次 read 必须报告「已变」，绝不回「内容未变」。"""
    p = fs.write("a.txt", "line1\nline2\nline3\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0

    p.write_bytes(b"line1\nline2\nEXTERNAL-MARK\n")   # 模拟别的程序/用户改动

    # 直接要 cached：磁盘 sha1 与缓存不同，必须立刻报「已变」，不许伪造命中。
    c = fs.run("read", "a.txt", "--mode", "cached")
    assert c.rc == 0
    assert "内容未变（sha1 相同）" not in c.text
    assert "内容已变（磁盘 sha1 与缓存不同）" in c.text

    # changed 模式把新正文/差异给出来（不返回旧的 line3）。
    r = fs.run("read", "a.txt", "--mode", "changed")
    assert r.rc == 0
    assert "内容未变（sha1 相同）" not in r.text
    assert "EXTERNAL-MARK" in r.text or "磁盘内容已变" in r.text

    # 缓存同步到新 sha1 后再来一次 cached：仍不能说「内容未变」——
    # 本会话从未完整交付过 v2 的正文，输出「本会话还没有完整交付过」同样是对的。
    c2 = fs.run("read", "a.txt", "--mode", "cached")
    assert c2.rc == 0
    assert "内容未变（sha1 相同）" not in c2.text


# ---------------------------------------------------------------- 占用 / 替换失败 / 权限

def test_win_occupied_file_read_ok_but_write_fails_cleanly(fs):
    """文件被别的进程占住（share=FILE_SHARE_READ）：可读、写失败、原文件保住、不挂死。"""
    p = fs.write("a.txt", "ORIGINAL\n")
    fs.write("new.txt", "NEWCONTENT\n")

    with _win_handle(p, share=FILE_SHARE_READ):
        r = fs.run("read", "a.txt", "--mode", "full")
        assert r.rc == 0 and "ORIGINAL" in r.text      # 读不受影响

        w = _bounded(fs, ["write", "a.txt", "--content-file", "new.txt"], timeout=15)
        assert w.rc == 2, w                            # os.replace 6 次重试后干净失败
        assert "[TIMEOUT]" not in w.text               # 没有退化成 30s 死锁
        assert "写不进" in w.text or "占用" in w.text or "读写失败" in w.text
        assert p.read_bytes() == b"ORIGINAL\n"         # 原文件一个字节没动
        assert _tmp_residue(fs) == []                  # 临时文件被收干净


def test_win_readonly_attribute_blocks_write_and_preserves_original(fs):
    """权限不足（只读属性）：edit 干净失败，原文件保住，无临时残留。"""
    p = fs.write("a.txt", "ORIGINAL\n")
    os.chmod(p, 0o444)
    try:
        r = _bounded(fs, ["edit", "a.txt", "--old", "ORIGINAL", "--new", "EDITED"],
                     timeout=15)
        assert r.rc == 2, r
        assert "[TIMEOUT]" not in r.text
        assert "写不进" in r.text or "占用" in r.text or "读写失败" in r.text
        assert p.read_bytes() == b"ORIGINAL\n"
        assert _tmp_residue(fs) == []
    finally:
        os.chmod(p, 0o666)                             # 还原，免得 tmp_path 清不掉


# ---------------------------------------------------------------- 异常退出 / 死锁

def test_win_crash_while_holding_lock_does_not_deadlock_next_edit(fs):
    """进程持 msvcrt 锁时崩溃：OS 自动释放锁，后续 edit 不死锁。"""
    p = fs.write("a.txt", "ORIGINAL\n")
    code = "\n".join([
        "import os, sys, importlib.util",
        "from pathlib import Path",
        f"spec = importlib.util.spec_from_file_location('fstate_crash', {str(FSTATE)!r})",
        "m = importlib.util.module_from_spec(spec)",
        "sys.modules['fstate_crash'] = m",            # dataclass 字符串注解需要先注册
        "spec.loader.exec_module(m)",
        f"m.CACHE_HOME = Path({str(fs.home)!r})",
        f"cm = m._cache_lock(Path({str(p)!r}))",
        "cm.__enter__()",
        "print('LOCKED', flush=True)",
        "os._exit(7)",                                # 不解锁、不 close、直接崩
    ])
    env = os.environ.copy()
    env["FSTATE_HOME"] = str(fs.home)
    env["DSH_SESSION_ID"] = fs.session
    env.pop("PYTHONIOENCODING", None)
    cp = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(fs.work),
                        capture_output=True, timeout=20)
    assert cp.returncode == 7, cp.stderr.decode("utf-8", "replace")
    assert b"LOCKED" in cp.stdout
    assert fs.lock_files(), "崩溃进程应留下 .lock 文件（证明 OS 锁真被拿过）"

    r = _bounded(fs, ["edit", "a.txt", "--old", "ORIGINAL", "--new", "EDITED"],
                 timeout=15)
    assert r.rc == 0, r
    assert "[TIMEOUT]" not in r.text                   # 核心：没死锁
    assert p.read_bytes() == b"EDITED\n"


# ---------------------------------------------------------------- 缓存 / 影子 异常恢复

def test_win_corrupted_json_is_healed_under_concurrent_reads(fs):
    """缓存 JSON 损坏 + 6 进程并发读：不自愈成半截，最终缓存 sha1 与磁盘一致。"""
    p = fs.write("a.txt", "hello\nworld\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0
    fs.set_meta_raw("{ this is not json")            # 模拟写一半崩溃

    results = fs.run_parallel(
        [["read", "a.txt", "--mode", "full"] for _ in range(6)], timeout=30)
    for r in results:
        assert r.rc == 0, r
        assert "[TIMEOUT]" not in r.text, r

    m = fs.meta()                                     # 合法 JSON
    assert m["sha1"] == hashlib.sha1(p.read_bytes()).hexdigest()
    assert _tmp_residue(fs) == []

    r3 = fs.run("read", "a.txt")
    assert r3.rc == 0
    assert "内容已变（磁盘 sha1 与缓存不同）" not in r3.text   # 恢复后不误报变化


def test_win_shadow_lost_external_change_falls_back_to_full_text(fs):
    """Shadow 丢失 + 外部改动：明确告知没有基线，并真给全文（不是前 400 行）。"""
    lines = "\n".join(f"L{i:04d}" for i in range(1, 502)) + "\n"
    p = fs.write("big.txt", lines)
    assert fs.run("read", "big.txt", "--mode", "full").rc == 0
    fs.drop_shadow()
    p.write_bytes(lines.replace("L0300", "CHANGED-MARK").encode("utf-8"))

    r = fs.run("read", "big.txt", "--mode", "changed")
    assert r.rc == 0
    assert "没有可用的旧版本快照" in r.text
    assert "给不了差异" in r.text
    assert "下面是全文" in r.text
    assert "CHANGED-MARK" in r.text                  # 降级确实把新正文给了出来
    assert "501|" in r.text                          # 最后一行也进来了，不是 400 行截断
    assert "不算读过" not in r.text                  # force_full 应真的记成完整交付


def test_win_shadow_corrupt_is_refused_and_not_silently_overwritten(fs):
    """Shadow 同行数篡改：拒绝编造假 diff，且不把篡改痕迹悄悄抹掉。"""
    fs.write("a.txt", "l1\nl2\nl3\nl4\nl5\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0
    fs.set_shadow("l1\nl2\nTAMPERED\nl4\nl5")       # 5 行不变，内容被改
    before = fs.read_shadow()

    r = fs.run("read", "a.txt", "--mode", "diff")
    assert r.rc == 0
    assert "TAMPERED" not in r.text                  # 不输出不存在的差异
    assert "没有可用的旧版本快照" in r.text
    assert "给不了差异" in r.text
    assert fs.read_shadow() == before                # 工具没偷偷重写 shadow 掩盖篡改


def test_win_stale_cache_tmp_is_cleaned_on_next_cache_write(fs):
    """崩溃残留的陈旧缓存临时文件（>1h）会在下一次缓存写入时被清掉。"""
    fs.write("a.txt", "hello\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0
    target = fs.meta_file().name
    stale = fs.home / f"{target}.fstate-tmp-99999-aabbcc"
    stale.write_bytes(b"half-written")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    assert fs.run("read", "a.txt", "--mode", "full").rc == 0   # 触发 _save_meta 清理
    assert not stale.exists()
    assert _tmp_residue(fs) == []
