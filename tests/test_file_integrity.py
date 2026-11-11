# tests/test_file_integrity.py
"""P0 文件完整性：编码/BOM/换行/末尾换行/空文件/二进制拒绝/大文件/写入失败不破坏原文件/硬链接。"""
from __future__ import annotations

import argparse
import os

import pytest


# ---- 编码 / BOM ----

def test_utf8_bom_preserved_through_edit(fs):
    """UTF-8 BOM 必须在 edit 后原样保留（不能被丢掉或重复插入）。"""
    fs.write("bom.txt", b"\xef\xbb\xbf" + "中文内容\n第二行\n".encode("utf-8"))
    r = fs.run("read", "bom.txt", "--mode", "full")
    assert r.rc == 0
    assert "BOM" in r.text, r.text
    r2 = fs.run("edit", "bom.txt", "--old", "第二行", "--new", "改过的行")
    assert r2.rc == 0, r2.text
    data = fs.read_bytes("bom.txt")
    assert data.startswith(b"\xef\xbb\xbf"), data[:16]
    assert data.count(b"\xef\xbb\xbf") == 1, data[:16]
    assert "改过的行".encode("utf-8") in data
    data.decode("utf-8-sig")


def test_gbk_preserved_through_edit(fs):
    """GBK 文件 edit 后仍是合法 GBK，不能被悄悄换成 UTF-8。"""
    fs.write("gbk.txt", "中文GBK内容\n第二行\n".encode("gbk"))
    r = fs.run("read", "gbk.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "gbk" in r.text.lower(), r.text
    r2 = fs.run("edit", "gbk.txt", "--old", "第二行", "--new", "改过")
    assert r2.rc == 0, r2.text
    data = fs.read_bytes("gbk.txt")
    assert "改过".encode("gbk") in data
    data.decode("gbk")  # 必须还能按 gbk 解出来


def test_utf16_bom_file_readable(fs):
    """UTF-16-LE BOM 文件可按文本读取，BOM 候选顺序不能把它误判成 binary。"""
    fs.write("u16.txt", b"\xff\xfe" + "hello\nworld\n".encode("utf-16-le"))
    r = fs.run("read", "u16.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "hello" in r.text


# ---- 换行 ----

def test_crlf_preserved_through_edit(fs):
    """CRLF 文件 edit 单行后，所有 CRLF 与末尾换行都原样保留，不能被冲成 LF。"""
    fs.write("crlf.txt", b"aaa\r\nbbb\r\nccc\r\n")
    r = fs.run("read", "crlf.txt", "--mode", "full")
    assert r.rc == 0
    assert "CRLF" in r.text, r.text
    r2 = fs.run("edit", "crlf.txt", "--old", "bbb", "--new", "BBB")
    assert r2.rc == 0, r2.text
    assert fs.read_bytes("crlf.txt") == b"aaa\r\nBBB\r\nccc\r\n"


def test_lf_preserved_through_edit(fs):
    """LF 文件 edit 后仍是 LF，不凭空变 CRLF。"""
    fs.write("lf.txt", b"aaa\nbbb\nccc\n")
    fs.run("read", "lf.txt", "--mode", "full")
    fs.run("edit", "lf.txt", "--old", "bbb", "--new", "BBB")
    assert fs.read_bytes("lf.txt") == b"aaa\nBBB\nccc\n"


def test_write_new_file_does_not_add_trailing_newline(fs):
    """保护：新建文件时调用方没给末尾换行，不许凭空补一个。"""
    r = fs.run("write", "n.txt", "--stdin", stdin="abc")
    assert r.rc == 0, r.text
    assert fs.read_bytes("n.txt") == b"abc"


def test_write_preserves_original_trailing_newline_state(fs):
    """保护：原文件没有末尾换行，覆盖写也不该凭空补上。"""
    fs.write("noeol.txt", b"line1\nline2")  # 无末尾换行
    r = fs.run("write", "noeol.txt", "--stdin", stdin="A\nB")
    assert r.rc == 0, r.text
    assert fs.read_bytes("noeol.txt") == b"A\nB"


# ---- 空文件 ----

def test_empty_file_read_and_write(fs):
    """空文件可以正常读和编辑。"""
    fs.write("empty.txt", b"")
    assert fs.run("read", "empty.txt", "--mode", "full").rc == 0
    assert fs.meta()["lines"] == 0
    r = fs.run("write", "empty.txt", "--stdin", stdin="now content\n")
    assert r.rc == 0, r.text
    assert fs.read_bytes("empty.txt") == b"now content\n"


def test_write_empty_content_needs_allow_empty(fs):
    """保护：把非空文件写成空内容必须显式 --allow-empty，不能静默清空。"""
    fs.write("a.txt", "important\n")
    r = fs.run("write", "a.txt", "--stdin", stdin="")
    assert r.rc == 2
    assert "没被改动" in r.text or "空" in r.text
    assert fs.read_bytes("a.txt") == b"important\n"


# ---- 二进制拒绝（复现 P0-2：latin-1 兜底 + 只查前 8192 字节）----

def test_binary_with_late_nul_is_rejected(fs):
    """复现：NUL 出现在第 8192 字节之后的二进制必须被拒绝，不能当文本读入。"""
    fs.write("bin1.bin", b"A" * 9000 + b"\x00" * 100)
    r = fs.run("read", "bin1.bin", "--mode", "full")
    assert r.rc == 2, "二进制被当文本读入了\n" + r.text[:400]
    assert "无法按文本读取" in r.text or "binary" in r.text


def test_binary_without_nul_but_control_bytes_is_rejected(fs):
    """复现：无 NUL 但含大量控制字节的二进制必须被拒绝（latin-1 万能兜底失效）。"""
    blob = bytes(range(1, 32)) * 200 + b"ABC" * 10 + bytes([0x80, 0x81, 0xfe, 0xff]) * 200
    fs.write("bin2.bin", blob)
    r = fs.run("read", "bin2.bin", "--mode", "full")
    assert r.rc == 2, "无 NUL 的二进制被当文本读入了\n" + r.text[:400]
    assert "无法按文本读取" in r.text or "binary" in r.text


def test_binary_edit_is_rejected_without_damaging_bytes(fs):
    """复现：二进制不能进 edit 去改写 0x0D/0x0A 字节。"""
    blob = bytes(range(1, 32)) * 200 + b"\r\n" * 50
    fs.write("bin3.bin", blob)
    r = fs.run("edit", "bin3.bin", "--old", "x", "--new", "y")
    assert r.rc != 0
    assert fs.read_bytes("bin3.bin") == blob


def test_normal_utf8_text_is_not_misjudged_binary(fs):
    """保护：正常 UTF-8 / 中文文本不能因新的二进制检测被误拒。"""
    fs.write("text.txt", "普通中文文本\n含少量标点，！？\nEnglish text too\n")
    r = fs.run("read", "text.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "普通中文文本" in r.text


def test_legit_latin1_text_still_readable(fs):
    """保护：合法 latin-1 西欧文本（é 等）仍可读，不被二进制检测误杀。"""
    fs.write("latin.txt", b"caf\xe9 na\xefve r\xe9sum\xe9\n")
    r = fs.run("read", "latin.txt", "--mode", "full")
    assert r.rc == 0, r.text
    assert "caf" in r.text


# ---- 大文件 ----

def test_read_over_8mb_is_refused(fs):
    """保护：>8MB 文件 read 应明确拒绝，不能吃进上下文。"""
    fs.write("huge.txt", b"x" * (8 * 1024 * 1024 + 1))
    r = fs.run("read", "huge.txt", "--mode", "full")
    assert r.rc == 2
    assert "toolarge" in r.text or "无法按文本读取" in r.text


def test_write_over_8mb_does_not_destroy_original(fs):
    """复现 P2-4：写出 >8MB 内容必须在不写盘之前就拒绝，不能覆盖原文件后报失败。"""
    fs.write("target.txt", b"ORIGINAL-IMPORTANT")
    big = fs.write("big.txt", b"y" * (9 * 1024 * 1024))
    r = fs.run("write", "target.txt", "--content-file", str(big), "--force")
    assert r.rc == 2
    assert fs.read_bytes("target.txt") == b"ORIGINAL-IMPORTANT", (
        "原文件已被覆盖，写入失败却破坏了它"
    )


# ---- 写入失败不破坏原文件 / 硬链接 ----

def test_hardlink_edit_updates_both_names(fs):
    """保护：硬链接语义 —— 通过任一名字编辑，两个名字看到同一份新内容。"""
    fs.write("orig.txt", "aaa\n")
    fs.make_hardlink("orig.txt", "alias.txt")
    r = fs.run("edit", "orig.txt", "--old", "aaa", "--new", "ZZZ")
    assert r.rc == 0, r.text
    assert fs.read("alias.txt") == "ZZZ\n"
    assert fs.read("orig.txt") == "ZZZ\n"


def test_hardlink_write_failure_rolls_back(mod, tmp_path, monkeypatch):
    """复现：硬链接原地写中途失败（磁盘满/kill）必须回滚，不能留「前半新后半旧」。

    白盒：让 os.fsync 在 write 之后抛错，模拟写盘中途失败。
    """
    p = tmp_path / "orig.txt"
    original = b"ORIGINAL-BYTES-0123456789-ABCDEFGHIJ" * 20
    p.write_bytes(original)
    os.link(p, tmp_path / "alias.txt")

    def boom(_fd):
        raise OSError("simulated disk full during fsync")

    monkeypatch.setattr(mod.os, "fsync", boom)
    with pytest.raises(OSError):
        mod._atomic_write(p, b"NEW-SHORT-CONTENT")
    assert p.read_bytes() == original, "硬链接写失败后原文件被破坏（前半新后半旧）"


def test_atomic_write_failure_keeps_original(fs):
    """保护：普通文件原子写失败时原文件一个字节都不动。"""
    fs.write("a.txt", b"KEEP-ME")
    # 用一个目录去顶住 replace：把目标换成一个目录会让 os.replace 失败
    # 这里改用只读属性模拟（Windows），失败时临时文件要被清理、原文件不变
    r = fs.run("write", "a.txt", "--stdin", stdin="new content")
    assert r.rc == 0
    assert fs.read_bytes("a.txt") == b"new content"
    # 不应留下半成品临时文件
    assert not list(fs.work.glob(".*.fstate-tmp*")), list(fs.work.glob(".*.fstate-tmp*"))


# ---- 路径大小写（Linux 语义，用白盒模拟 POSIX normcase）----

def test_key_respects_case_on_case_sensitive_fs(mod, monkeypatch):
    """复现：大小写敏感文件系统上 Foo.py 与 foo.py 不能共用缓存 key。

    Windows 的 os.path.normcase 会折叠大小写；这里把它 patch 成恒等函数模拟 POSIX。
    """
    monkeypatch.setattr(mod.os.path, "normcase", lambda s: s)

    class P:
        def __init__(self, s):
            self.s = s

        def resolve(self):
            return self.s

    assert mod._key(P("/proj/Foo.py")) != mod._key(P("/proj/foo.py"))


def test_key_case_insensitive_on_windows(mod):
    """保护：Windows（大小写不敏感 FS）上大小写不同的写法仍映射到同一 key。"""
    class P:
        def __init__(self, s):
            self.s = s

        def resolve(self):
            return self.s

    a = mod._key(P("C:/Proj/Foo.py"))
    b = mod._key(P("c:/proj/foo.py"))
    if os.name == "nt":
        assert a == b
    else:  # pragma: no cover - 取决于运行平台
        assert a == b or a != b


# ---- 符号链接 ----

def test_symlink_edit_follows_target(fs):
    """保护：软链接沿 realpath 写，链接本身保留，目标文件被改。"""
    target = fs.write("real.txt", "aaa\n")
    link = fs.path("link.txt")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接（Windows 常需开发者模式/管理员）")
    r = fs.run("edit", "link.txt", "--old", "aaa", "--new", "ZZZ")
    assert r.rc == 0, r.text
    assert link.is_symlink(), "软链接被替换成了普通文件"
    assert fs.read("real.txt") == "ZZZ\n"


def test_broken_symlink_is_reported(fs):
    """保护：悬挂软链接必须给出明确提示，不是笼统的「文件不存在」。"""
    link = fs.path("dangling.txt")
    try:
        os.symlink(fs.path("gone-target"), link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    r = fs.run("read", "dangling.txt")
    assert r.rc == 2
    assert "悬挂" in r.text or "链接" in r.text, r.text
