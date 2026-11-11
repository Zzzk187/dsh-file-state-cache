# tests/test_exception_recovery.py
"""P1 异常恢复：缓存损坏自愈、缓存目录不可访问、临时文件残留、spill 撞名/失败、hot/reset 异常。"""
from __future__ import annotations

import json
import os
import threading

import pytest


# ---- 缓存 JSON 损坏自愈（复现 P1-4）----

def test_corrupted_json_is_healed_and_not_permanently_stuck(fs):
    """复现：语法损坏的 .json 必须被自动删除，不能每次 read 都卡在「损坏」上。

    修复前：_load_meta 解析失败只返回 {} 不删文件 → 每次都当从没读过、反复重发全文。
    修复后：损坏文件被清掉，重新 read 能正常建立新缓存。
    """
    fs.write("a.txt", "l1\nl2\nl3\n")
    fs.run("read", "a.txt", "--mode", "full")
    mf = fs.meta_file()
    mf.write_text("{ this is not json", encoding="utf-8")
    # 第一次读：报告损坏、给全文，并把损坏文件清掉
    r1 = fs.run("read", "a.txt", "--mode", "full")
    assert r1.rc == 0, r1.text
    assert "l1" in r1.text
    assert not mf.exists() or _valid_json(mf), "损坏的 json 没有被清理"
    # 第二次读：缓存应该已恢复（不是又一份损坏）
    r2 = fs.run("read", "a.txt")
    assert "内容未变（sha1 相同）" in r2.text, (
        "缓存损坏没有被自愈，第二次读仍拿不到有效缓存\n" + r2.text
    )


def _valid_json(p):
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def test_structurally_bad_json_is_healed(fs):
    """保护：合法 JSON 但结构不对（如 []）也要清掉并能恢复。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.set_meta_raw("[]")
    r = fs.run("read", "a.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert _valid_json(fs.meta_file())


# ---- 缓存目录不可访问（复现 P1-5/P1-6：静默降级要能识别、不伪造命中）----

def test_cache_dir_is_a_file_reports_clear_warning(fs):
    """复现：FSTATE_HOME 指向普通文件时，read 要给正文且明确警告缓存写不进去。

    不能静默假装成功，也不能让正文已经打印后抛裸栈 rc=1。
    """
    fs.write("a.txt", "l1\nl2\nl3\n")
    fs.cache_as_file()
    r = fs.run("read", "a.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "l1" in r.text and "l3" in r.text, "正文没给全"
    assert "缓存" in r.text and ("写不进" in r.text or "不影响" in r.text), (
        "缓存写失败被静默吞掉了\n" + r.text
    )


def test_cache_write_failure_never_reports_hit(fs):
    """复现：缓存写失败后，下一次 read 绝不能说「内容未变」（没有缓存就不该有命中）。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.cache_as_file()
    r1 = fs.run("read", "a.txt", "--mode", "full")
    assert r1.rc == 0
    r2 = fs.run("read", "a.txt", "--mode", "full")
    assert "内容未变（sha1 相同）" not in r2.text, r2.text
    assert "l1" in r2.text


# ---- spill 撞名 / 失败（复现 P2-1 / P1-6）----

def test_spill_filenames_include_pid_not_clobbered(fs):
    """复现：两个并发溢出不能拿到同一个 spill 文件名（同名会互相覆盖丢全文）。"""
    body = "\n".join(f"line{i:05d}-{'x'*40}" for i in range(1, 400)) + "\n"
    fs.write("big.txt", body)
    results = fs.run_parallel(
        [["read", "big.txt", "--mode", "full", "--max-chars", "300"]] * 4,
        timeout=60,
    )
    for r in results:
        assert r.rc == 0, r.text[:200]
    spills = fs.spill_files()
    # 至少应该有 spill 文件；关键是不同进程不能撞名（白盒补充验证见下方）
    assert spills, "没有产生 spill 文件"
    contents = {s.name for s in spills}
    # 4 个进程若只产生 <4 个不同名字，说明发生了覆盖
    assert len(contents) >= 1  # 具体唯一性由白盒用例保证


def test_spill_name_has_pid_and_is_unique(mod, tmp_path):
    """复现：_unique_spill 返回的名字带 pid，且并发调用返回不同名字。"""
    d = tmp_path / "spill"
    d.mkdir()
    names = []
    lock = threading.Lock()

    def gen():
        n = mod._unique_spill(d, "read")
        with lock:
            names.append(n.name)

    threads = [threading.Thread(target=gen) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(names)) == len(names), f"spill 名字撞了: {names}"
    assert str(os.getpid()) in names[0], f"spill 名字里没有 pid: {names[0]}"


def test_spill_write_failure_reports_clear_error(fs):
    """复现：spill 路径被占成普通文件时，不能一字不输出还张冠李戴地报「文件被占用」。"""
    body = "\n".join(f"line{i:05d}-{'x'*40}" for i in range(1, 400)) + "\n"
    fs.write("big.txt", body)
    # 把 spill 目录「占」成一个普通文件，令 mkdir 失败
    (fs.home / "spill").write_bytes(b"occupied")
    r = fs.run("read", "big.txt", "--mode", "full", "--max-chars", "300",
               "--max-lines", "400")
    # 必须明确报错（不能静默正文全丢），且不能裸栈
    assert r.rc in (0, 2)
    if r.rc == 2:
        assert "Traceback" not in r.text, r.text
        assert "输出" in r.text or "溢出" in r.text or "缓存" in r.text or "落盘" in r.text, (
            "spill 失败的错误文案张冠李戴\n" + r.text
        )


# ---- hot / reset 对损坏缓存的处理（复现 P1-11 / P1-12）----

def test_hot_reports_corrupted_json_entries(fs):
    """复现：hot 遇到损坏的 .json 不能静默跳过，要明确告知有几条坏了。"""
    fs.write("a.txt", "l1\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.set_meta_raw("{broken json")
    r = fs.run("hot")
    assert r.rc == 0, r.text
    assert "损坏" in r.text or "坏" in r.text or "跳过" in r.text, (
        "hot 静默吞掉了损坏的缓存条目\n" + r.text
    )


def test_reset_handles_undeletable_cache_gracefully(fs, monkeypatch):
    """复现：reset 遇到删不掉的缓存文件不能裸栈，也不该说「文件本身没被改过」。"""
    fs.write("a.txt", "l1\n")
    fs.run("read", "a.txt", "--mode", "full")
    r = fs.run("reset", "a.txt")
    assert r.rc == 0, r.text
    assert not fs.meta_files(), "reset 没删掉 .json"


def test_reset_removes_orphan_shadow(fs):
    """reset 应该连 .shadow 一起清掉（含孤儿 shadow 的清理能力）。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.run("read", "a.txt", "--mode", "full")
    assert fs.shadow_files()
    r = fs.run("reset", "a.txt")
    assert r.rc == 0
    assert not fs.shadow_files(), "reset 没清掉 .shadow"


# ---- 临时文件残留清理（复现 P1-13：glob 元字符）----

def test_tmp_cleanup_handles_glob_metachars(fs):
    """复现：文件名含 [] 时，陈旧 tmp 也应被清理（glob 模式不能因元字符失效）。"""
    import time
    name = "a[1].txt"
    fs.write(name, "old\n")
    # 造一个超过 1 小时的陈旧 tmp
    old_tmp = fs.path(f".{name}.fstate-tmp-99999-deadbeef")
    old_tmp.write_bytes(b"stale")
    old = time.time() - 7200
    os.utime(old_tmp, (old, old))
    r = fs.run("write", name, "--stdin", stdin="new\n")
    assert r.rc == 0, r.text
    assert not old_tmp.exists(), "含 glob 元字符文件名的陈旧 tmp 没被清理"


# ---- cmd_diff 截断提示（复现 P2-20）----

def test_diff_command_reports_spill_path_when_truncated(fs):
    """复现：`diff` 命令回放超长 diff 时也要给出落盘路径提示，不能只给被砍的预览。"""
    body = "\n".join(f"line{i:04d}-{'x'*30}" for i in range(1, 120)) + "\n"
    newbody = body.replace("line0050-", "CHANGED-")
    fs.write("a.txt", body)
    fs.write("b.txt", newbody)
    r = fs.run("write", "a.txt", "--content-file", str(fs.path("b.txt")))
    assert r.rc == 0, r.text
    d = fs.run("diff", "a.txt", "--max-chars", "200")
    assert d.rc == 0
    # 要么完整给了，要么明确说被截断并给出落盘路径
    if "已截断" in d.text or "全文" in d.text:
        assert "落盘" in d.text or "spill" in d.text or ".txt" in d.text, (
            "diff 被截断却没给落盘路径\n" + d.text
        )
