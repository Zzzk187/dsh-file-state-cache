# tests/test_concurrency.py
"""P0 并发安全：多进程/多线程同时操作同一文件、缓存元数据与临时文件。"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def _edit_ns(path, old, new):
    return argparse.Namespace(
        path=str(path), old_file="", old=old, new_file="", new=new,
        all=False, reason="", label="", max_chars=12000,
    )


# ---- 复现 P0-3：edit 无锁「读全文→替换→整篇写回」→ lost update ----

def test_concurrent_edit_serialized_and_no_lost_update(mod, tmp_path, monkeypatch):
    """复现：两个线程同时 edit 同一文件的不同位置，修复前必然互相覆盖。

    做法：monkeypatch _read_once()，让**第一次**读睡 0.4 秒，制造确定的重叠窗口。
    修复前（无锁）：线程 B 在 A 睡觉期间完成整篇写回，A 醒来后用旧 text 覆盖
    → B 的改动丢失，且读的并发进入数为 2。
    修复后（文件锁）：B 阻塞在锁上，等 A 整段「读→写→更新缓存」做完才进入，
    读的并发进入数最大为 1，两个改动都保留。
    """
    work = tmp_path / "work"
    work.mkdir()
    p = work / "a.txt"
    p.write_text("aaa\nbbb\nccc\n", encoding="utf-8")

    # 1.1.0 起 cmd_edit 走 _read_once（sha1 与正文同源），不再经 load；
    # 探针必须挂在真正被调用的读取入口上，否则测不到锁的串行化效果。
    real_read_once = mod._read_once
    gate = threading.Lock()
    state = {"calls": 0, "inflight": 0, "max_inflight": 0}

    def slow_read_once(path):
        with gate:
            state["calls"] += 1
            first = state["calls"] == 1
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        try:
            if first:
                time.sleep(0.4)
            return real_read_once(path)
        finally:
            with gate:
                state["inflight"] -= 1

    monkeypatch.setattr(mod, "_read_once", slow_read_once)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(mod.cmd_edit, _edit_ns(p, "aaa", "AAA"))
        f2 = ex.submit(mod.cmd_edit, _edit_ns(p, "ccc", "CCC"))
        rc1, rc2 = f1.result(), f2.result()

    assert rc1 == 0 and rc2 == 0, (rc1, rc2)
    body = p.read_text(encoding="utf-8")
    assert "AAA" in body and "CCC" in body, (
        f"并发 edit 丢失改动（lost update），最终内容:\n{body}"
    )
    assert state["max_inflight"] == 1, (
        f"两个 edit 的「读」重叠了（max_inflight={state['max_inflight']}），没有被串行化"
    )


def test_concurrent_read_does_not_corrupt_meta(fs):
    """多进程并发首次 read 同一文件：缓存 JSON 必须始终可解析、state 正常。"""
    fs.write("a.txt", "\n".join(f"L{i}" for i in range(1, 31)) + "\n")
    results = fs.run_parallel([["read", "a.txt", "--mode", "full"]] * 12, timeout=60)
    for r in results:
        assert r.rc == 0, r.text[:300]
        assert "[TIMEOUT]" not in r.text, "并发 read 卡死了（疑似死锁）"
    # 元数据必须合法 JSON，且能被 state 正常读出
    m = fs.meta()
    assert isinstance(m.get("version"), int)
    s = fs.run("state", "a.txt")
    assert s.rc == 0
    assert json.loads(s.out)["cache_state"] == "fresh"


def test_concurrent_edit_leaves_cache_usable(fs):
    """多进程并发 edit 同一文件：不崩溃、不死锁、缓存仍可用、文件不是半截。"""
    fs.write("a.txt", "\n".join(f"key{i}=val{i}" for i in range(1, 9)) + "\n")
    argvs = [["edit", "a.txt", "--old", f"key{i}=val{i}",
              "--new", f"key{i}=EDITED{i}"] for i in range(1, 9)]
    results = fs.run_parallel(argvs, timeout=90)
    for r in results:
        assert "[TIMEOUT]" not in r.text, "并发 edit 卡死（疑似死锁）"
        assert r.rc in (0, 3), r.text[:300]  # 3 = 片段不唯一/没找到，属于并发下的可解释结果
    body = fs.read("a.txt")
    # 文件结构必须完整（8 行、每行都是 keyN=...），不能是半截/混合内容
    lines = body.splitlines()
    assert len(lines) == 8, lines
    for i, ln in enumerate(lines, 1):
        assert ln.startswith(f"key{i}="), ln
    # 缓存必须仍可解析
    fs.meta()
    assert fs.run("state", "a.txt").rc == 0


def test_concurrent_edit_preserves_all_changes(fs):
    """多进程并发 edit 不同唯一片段：加锁后所有改动都应保留（不丢）。"""
    fs.write("a.txt", "\n".join(f"token{i}" for i in range(1, 9)) + "\n")
    argvs = [["edit", "a.txt", "--old", f"token{i}",
              "--new", f"TOKEN{i}"] for i in range(1, 9)]
    results = fs.run_parallel(argvs, timeout=90)
    assert all("[TIMEOUT]" not in r.text for r in results), "并发 edit 卡死"
    body = fs.read("a.txt")
    for i in range(1, 9):
        assert f"TOKEN{i}" in body, f"第 {i} 个改动被并发覆盖了:\n{body}"


def test_no_lock_file_left_in_workdir(fs):
    """锁文件只放在缓存目录，不能污染用户源码目录。"""
    fs.write("a.txt", "x\n")
    fs.run("edit", "a.txt", "--old", "x", "--new", "y")
    assert not list(fs.work.glob("*.lock")), list(fs.work.glob("*.lock"))
    assert not list(fs.work.glob(".*.fstate-*")), list(fs.work.glob(".*.fstate-*"))
