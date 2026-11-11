# tests/test_cache_correctness.py
"""P0 缓存正确性：首次/重复读、修改/删除/重建/截断、跨会话凭证、shadow 完整性。

每个用例都是黑盒 CLI 调用（真实子进程 + 独立 FSTATE_HOME）。
标注「复现」的用例在修复前会 FAIL；标注「保护」的用例是既有正确行为，防止回归。
"""
from __future__ import annotations

import json

import pytest


LINES_500 = "\n".join(f"line{i:04d}" for i in range(1, 501)) + "\n"


def test_first_read_delivers_full_body(fs):
    """首次读必须把全文交出去，不能说「内容未变」。"""
    fs.write("a.txt", "l1\nl2\nl3\n")
    r = fs.run("read", "a.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "内容未变" not in r.text
    assert "l1" in r.text and "l2" in r.text and "l3" in r.text
    assert "版本 v1" in r.text
    m = fs.meta()
    assert m["sha1"] and m["version"] == 1
    assert m["full_version"] == 1


def test_repeat_read_hits_cached_same_session(fs):
    """保护：同会话重复 auto 读应命中 cached（省 token 的核心场景，不能退化）。"""
    fs.write("a.txt", "l1\nl2\nl3\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0
    r = fs.run("read", "a.txt")
    assert r.rc == 0
    assert "内容未变（sha1 相同）" in r.text, r.text
    assert "本会话已完整交付过" in r.text


def test_external_modification_never_returns_old_content(fs):
    """保护：外部编辑后 auto 读必须给差异/新内容，不能回「内容未变」。"""
    fs.write("a.txt", "l1\nl2\nl3\n")
    assert fs.run("read", "a.txt", "--mode", "full").rc == 0
    fs.write("a.txt", "l1\nCHANGED\nl3\n")
    r = fs.run("read", "a.txt")
    assert r.rc == 0
    assert "内容未变" not in r.text, r.text
    assert "CHANGED" in r.text


def test_explicit_cached_mode_after_change_never_lies(fs):
    """保护：显式 --mode cached 在磁盘已变时必须说「已变」，不能伪造 sha1 相同。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.write("a.txt", "l1\nNEW\n")
    r = fs.run("read", "a.txt", "--mode", "cached")
    assert "内容已变（磁盘 sha1 与缓存不同）" in r.text, r.text
    assert "内容未变（sha1 相同）" not in r.text


def test_delete_file_is_detected(fs):
    """删除：read 报不存在，state 报 MISSING，缓存记录不丢。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.unlink("a.txt")
    r = fs.run("read", "a.txt")
    assert r.rc == 2
    assert "文件不存在" in r.text
    s = fs.run("state", "a.txt")
    assert s.rc == 0
    assert json.loads(s.out)["cache_state"].startswith("MISSING")


def test_rebuild_with_different_content_is_detected(fs):
    """删除后用不同内容重建：sha1 变化必须被识别，绝不返回旧内容。"""
    fs.write("a.txt", "old1\nold2\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.unlink("a.txt")
    fs.write("a.txt", "brand new content\nsecond line\n")
    r = fs.run("read", "a.txt")
    assert "内容未变" not in r.text, r.text
    assert "brand new content" in r.text or "second line" in r.text or "## 磁盘内容已变" in r.text


def test_rebuild_with_same_content_is_fresh_after_full_read(fs):
    """同内容重建且重新全文读过：缓存 fresh，不报 STALE。"""
    fs.write("a.txt", "same\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.unlink("a.txt")
    fs.write("a.txt", "same\n")
    fs.run("read", "a.txt", "--mode", "full")
    s = json.loads(fs.run("state", "a.txt").out)
    assert s["cache_state"] == "fresh"


def test_truncated_read_is_not_marked_as_read(fs):
    """保护：--max-lines 截断的读不能记为已读，下次还要给全文。"""
    fs.write("a.txt", "\n".join(f"L{i}" for i in range(1, 21)) + "\n")
    r = fs.run("read", "a.txt", "--mode", "full", "--max-lines", "3")
    assert r.rc == 0
    assert "不算读过" in r.text or "未完整进入上下文" in r.text, r.text
    m = fs.meta()
    assert m.get("full_version") is None, m
    r2 = fs.run("read", "a.txt")
    assert "内容未变" not in r2.text, r2.text


def test_range_read_does_not_grant_full_read_credential(fs):
    """保护：局部读不能伪造「已读全文」凭证。"""
    fs.write("a.txt", "\n".join(f"L{i}" for i in range(1, 51)) + "\n")
    r = fs.run("read", "a.txt", "--mode", "range", "--start", "1", "--end", "5")
    assert r.rc == 0
    m = fs.meta()
    assert m.get("full_version") is None, m
    assert m["read_ranges"] == [[1, 5]]


def test_cross_session_never_reuses_credential(fs):
    """保护：会话 A 全文读，会话 B 首次读必须重新给全文（凭证不能跨会话冒用）。"""
    fs.write("a.txt", "l1\nl2\nl3\n")
    fs.run("read", "a.txt", "--mode", "full", session="SESS-A")
    r = fs.run("read", "a.txt", session="SESS-B")
    assert "内容未变" not in r.text, r.text
    assert "l1" in r.text and "l3" in r.text


def test_missing_session_id_is_conservative(fs):
    """保护：DSH_SESSION_ID 缺失时必须重新给全文，不能命中 cached。"""
    fs.write("a.txt", "l1\nl2\n")
    fs.run("read", "a.txt", "--mode", "full", session="SESS-A")
    r = fs.run("read", "a.txt", session="")
    assert "内容未变" not in r.text, r.text
    assert "l1" in r.text


# ---- 复现：跨会话 + 非全文读 → 伪造缓存命中（P0-1，两份独立审计的头号发现）----

def test_cross_session_partial_read_must_not_forge_hit(fs):
    """复现 P0-1：会话 A 全文读 → 会话 B 只读 5 行 → 会话 B 再 read 不得说「本会话已完整交付过」。

    根因：_bump 每次读都把 session 刷新成当前会话，却保留旧会话签发的 full_version，
    于是新会话的上下文里其实没有全文，却被判定「已完整交付过」并永久跳过。
    """
    fs.write("big.txt", LINES_500)
    r1 = fs.run("read", "big.txt", "--mode", "full", session="SESS-A")
    assert r1.rc == 0 and "line0500" in r1.text
    r2 = fs.run("read", "big.txt", "--mode", "range", "--start", "1", "--end", "5",
                session="SESS-B")
    assert r2.rc == 0
    assert "line0500" not in r2.text  # 只给了 5 行
    r3 = fs.run("read", "big.txt", session="SESS-B")
    assert "内容未变（sha1 相同）" not in r3.text, (
        "伪造缓存命中：会话 B 从未拿到全文却被告知「本会话已完整交付过」\n" + r3.text
    )
    # 核心是「降级为重新读取」：B 确实重新拿到正文、且明确没被记为已读。
    # auto 模式对 >400 行文件按既有设计只给前 400 行（DEFAULT_MAX_LINES），
    # 与跨会话无关；要全文须像会话 A 那样显式 --mode full。
    assert "line0001" in r3.text, r3.text
    assert "不算读过" in r3.text or "未完整进入上下文" in r3.text, r3.text
    # 新会话显式要全文时，绝不能被旧会话的凭证挡住 —— 必须拿得到第 500 行。
    rf = fs.run("read", "big.txt", "--mode", "full", session="SESS-B")
    assert "line0500" in rf.text, rf.text


def test_cross_session_diff_read_must_not_forge_hit(fs):
    """复现 P0-1 变体：新会话只做 --mode diff，也不该拿到全文凭证。"""
    fs.write("big.txt", LINES_500)
    fs.run("read", "big.txt", "--mode", "full", session="SESS-A")
    fs.run("read", "big.txt", "--mode", "diff", session="SESS-B")
    r = fs.run("read", "big.txt", session="SESS-B")
    assert "内容未变（sha1 相同）" not in r.text, r.text
    assert "line0001" in r.text, r.text  # 降级重读，拿到了正文
    rf = fs.run("read", "big.txt", "--mode", "full", session="SESS-B")
    assert "line0500" in rf.text, rf.text


def test_cross_session_truncated_full_read_must_not_forge_hit(fs):
    """复现 P0-1 变体：新会话做被截断的 --mode full，也不该拿到全文凭证。"""
    fs.write("big.txt", LINES_500)
    fs.run("read", "big.txt", "--mode", "full", session="SESS-A")
    fs.run("read", "big.txt", "--mode", "full", "--max-lines", "10", session="SESS-B")
    r = fs.run("read", "big.txt", session="SESS-B")
    assert "内容未变（sha1 相同）" not in r.text, r.text
    assert "line0001" in r.text, r.text  # 降级重读，拿到了正文
    rf = fs.run("read", "big.txt", "--mode", "full", session="SESS-B")
    assert "line0500" in rf.text, rf.text


# ---- 复现：shadow 内容失真（同行数篡改）编造假 diff ----

def test_shadow_tampered_same_line_count_never_forges_diff(fs):
    """复现 P0-2：shadow 被改内容但行数不变，不能拿它编造不存在的差异。

    根因：_load_shadow 只校验行数，不校验内容。磁盘一个字节没动时，
    --mode diff 会输出一段根本不存在的差异，还可能据此刷新凭证。
    """
    fs.write("a.txt", "l1\nl2\nl3\nl4\nl5\n")
    fs.run("read", "a.txt", "--mode", "full")
    # 篡改 shadow：把第 3 行换掉，行数仍是 5
    fs.set_shadow("l1\nl2\nTAMPERED\nl4\nl5")
    r = fs.run("read", "a.txt", "--mode", "diff")
    assert "TAMPERED" not in r.text, ("用被篡改的快照编造了假差异\n" + r.text)
    assert "没有可用的旧版本快照" in r.text or "给不了差异" in r.text, r.text


def test_shadow_tampered_does_not_grant_credential(fs):
    """复现 P0-2 串联：假 diff 之后不能给新会话发免读凭证。"""
    fs.write("a.txt", "l1\nl2\nl3\nl4\nl5\n")
    fs.run("read", "a.txt", "--mode", "full", session="SESS-A")
    fs.set_shadow("l1\nl2\nTAMPERED\nl4\nl5")
    fs.run("read", "a.txt", "--mode", "diff", session="SESS-B")
    r = fs.run("read", "a.txt", session="SESS-B")
    assert "内容未变（sha1 相同）" not in r.text, r.text
    assert "l3" in r.text


def test_shadow_truncated_is_degraded(fs):
    """保护：shadow 行数不符（写入被打断）必须降级、明确告知，不能编造 diff。"""
    fs.write("a.txt", "l1\nl2\nl3\nl4\nl5\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.set_shadow("l1\nl2")
    r = fs.run("read", "a.txt", "--mode", "diff")
    assert "没有可用的旧版本快照" in r.text, r.text
    assert "不完整" in r.text or "给不了差异" in r.text


def test_shadow_missing_is_explicitly_reported(fs):
    """保护：没有 Shadow 基线必须明确告知用户。"""
    fs.write("a.txt", "l1\nl2\nl3\n")
    fs.run("read", "a.txt", "--mode", "full")
    fs.drop_shadow()
    r = fs.run("read", "a.txt", "--mode", "diff")
    assert "没有可用的旧版本快照" in r.text, r.text


def test_no_shadow_baseline_falls_back_to_full_text(fs):
    """保护 + 修复：无基线降级时必须给「全文」，不能只给前 400 行（P1-4）。"""
    body = "\n".join(f"line{i:04d}" for i in range(1, 502)) + "\n"
    fs.write("big.txt", body)
    fs.run("read", "big.txt", "--mode", "full")
    fs.drop_shadow()
    fs.write("big.txt", body.replace("line0300", "CHANGED_MARK"))
    r = fs.run("read", "big.txt")
    assert r.rc == 0
    # 降级为全文：应该能看到最后一行的行号正文，而不是「已显示前 400/501 行」
    assert "501|" in r.text or "不算读过" not in r.text, (
        "承诺给全文却只给了局部\n" + r.text[:600]
    )
    assert "没有可用的旧版本快照" in r.text


def test_shadow_roundtrip_with_trailing_blank_line(fs):
    """复现 P1-3：以空行结尾的文件，shadow round-trip 后行数不能丢，否则 diff 永久降级。"""
    fs.write("blank.txt", "a\nb\n\n")  # split_lines -> 3 行（最后一个是空行）
    r1 = fs.run("read", "blank.txt", "--mode", "full")
    assert r1.rc == 0
    assert fs.meta()["lines"] == 3, fs.meta()
    fs.write("blank.txt", "a\nCHANGED\n\n")
    r = fs.run("read", "blank.txt")
    assert "CHANGED" in r.text
    assert "## 磁盘内容已变" in r.text, (
        "shadow round-trip 丢了末尾空行，导致 diff 永久降级为全文\n" + r.text
    )


def test_empty_file_full_read_roundtrip(fs):
    """保护：空文件可以正常读；之后外部写入内容应被识别。"""
    fs.write("empty.txt", "")
    r = fs.run("read", "empty.txt", "--mode", "full")
    assert r.rc == 0
    assert fs.meta()["lines"] == 0
    fs.write("empty.txt", "now has content\n")
    r2 = fs.run("read", "empty.txt")
    assert "内容未变" not in r2.text
    assert "now has content" in r2.text or "## 磁盘内容已变" in r2.text


def test_state_full_read_valid_tracks_session(fs):
    """state 的 full_read_valid 必须与会话一致。"""
    fs.write("a.txt", "x\n")
    fs.run("read", "a.txt", "--mode", "full", session="SESS-A")
    valid_a = json.loads(fs.run("state", "a.txt", session="SESS-A").out)["full_read_valid"]
    valid_b = json.loads(fs.run("state", "a.txt", session="SESS-B").out)["full_read_valid"]
    assert valid_a is True
    assert valid_b is False
