# tests/conftest.py
"""黑盒 CLI 测试基础设施。

每次调用都是真实的 `python fstate.py ...` 子进程，环境变量里给一个**独立**的
FSTATE_HOME（缓存目录）和 DSH_SESSION_ID（会话凭证）。这样测试之间完全隔离，
也顺带验证了 CLI 的真实行为/退出码/输出文案，而不是绕过 argparse 直接调函数。
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FSTATE = ROOT / "scripts" / "fstate.py"

_MOD_COUNTER = itertools.count()


def _as_text(b) -> str:
    """ CompletedProcess 的 stdout/stderr 可能是 bytes（bytes 模式）或 str。 """
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b or ""


class CLIResult:
    def __init__(self, cp: subprocess.CompletedProcess = None, *, rc=0, out="", err=""):
        if cp is not None:
            self.rc = cp.returncode
            self.out = _as_text(cp.stdout)
            self.err = _as_text(cp.stderr)
        else:
            self.rc, self.out, self.err = rc, _as_text(out), _as_text(err)

    @property
    def text(self) -> str:
        return (self.out or "") + (self.err or "")

    def __repr__(self) -> str:
        return f"<CLIResult rc={self.rc} out={self.out!r} err={self.err!r}>"


class FS:
    """一个隔离的测试世界：工作目录 + 缓存目录 + 默认会话。"""

    def __init__(self, tmp_path: Path, session: str = "SESS-A"):
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.home = tmp_path / "cache"
        self.home.mkdir()
        self.session = session

    def _env(self, session):
        env = os.environ.copy()
        env["FSTATE_HOME"] = str(self.home)
        env["DSH_SESSION_ID"] = self.session if session is ... else (session or "")
        env.pop("PYTHONIOENCODING", None)
        return env

    # ---- CLI ----
    def run(self, *args, session=..., stdin=None, extra_env=None, cwd=None) -> CLIResult:
        env = self._env(session)
        if extra_env:
            env.update(extra_env)
        # 用 bytes 模式而非 text=True：Windows 的 TextIOWrapper 会把 str stdin 的
        # \n 翻译成 \r\n，导致 --stdin 写入的内容被平台换行污染（测试设施假象）。
        raw_stdin = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        cp = subprocess.run(
            [sys.executable, str(FSTATE), *[str(a) for a in args]],
            env=env,
            cwd=str(cwd or self.work),
            input=raw_stdin,
            capture_output=True,
        )
        return CLIResult(cp)

    # ---- 文件 ----
    def path(self, name) -> Path:
        return self.work / name

    def write(self, name, data) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            # newline="" -> 文本里的 \n / \r\n 原样落盘，不被平台换行转换污染
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(data)
        return p

    def write_bytes(self, name, data: bytes) -> Path:
        return self.write(name, data)

    def read_bytes(self, name) -> bytes:
        return self.path(name).read_bytes()

    def read(self, name) -> str:
        return self.path(name).read_text(encoding="utf-8")

    def exists(self, name) -> bool:
        return self.path(name).exists()

    def unlink(self, name) -> None:
        self.path(name).unlink()

    # ---- 缓存 ----
    def meta_files(self):
        return sorted(self.home.glob("*.json"))

    def shadow_files(self):
        return sorted(self.home.glob("*.shadow"))

    def lock_files(self):
        return sorted(self.home.glob("*.lock"))

    def meta_file(self) -> Path:
        fs = self.meta_files()
        assert len(fs) == 1, f"期望恰好 1 个 .json，实际 {fs}"
        return fs[0]

    def shadow_file(self) -> Path:
        fs = self.shadow_files()
        assert len(fs) == 1, f"期望恰好 1 个 .shadow，实际 {fs}"
        return fs[0]

    def meta(self) -> dict:
        return json.loads(self.meta_file().read_text(encoding="utf-8"))

    def raw_meta(self) -> str:
        return self.meta_file().read_text(encoding="utf-8")

    def set_meta_raw(self, text: str) -> None:
        self.meta_file().write_text(text, encoding="utf-8")

    def spill_files(self):
        d = self.home / "spill"
        return sorted(d.glob("*")) if d.is_dir() else []

    # ---- 故障注入 / 篡改 ----
    def set_shadow(self, text: str) -> None:
        """直接改写影子副本内容（模拟同步盘回退 / 人工篡改 / 半截写）。"""
        self.shadow_file().write_text(text, encoding="utf-8")

    def read_shadow(self) -> str:
        return self.shadow_file().read_text(encoding="utf-8")

    def drop_shadow(self) -> None:
        p = self.shadow_file()
        if p.exists():
            p.unlink()

    def make_hardlink(self, name, alias) -> Path:
        """造一个硬链接（同一 inode 两个名字）。"""
        import os as _os
        src, dst = self.path(name), self.path(alias)
        _os.link(src, dst)
        return dst

    def cache_as_file(self):
        """把缓存目录换成一个普通文件，模拟「缓存目录不可访问」。"""
        import shutil
        shutil.rmtree(self.home)
        self.home.write_bytes(b"not a dir")
        return self.home

    def run_parallel(self, argvs, session=..., timeout=60):
        """同时启动多个 CLI 进程（真·多进程），返回等长的结果列表。"""
        env = self._env(session)
        procs = [
            subprocess.Popen(
                [sys.executable, str(FSTATE), *[str(a) for a in argv]],
                env=env, cwd=str(self.work),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for argv in argvs
        ]
        out = []
        for p in procs:
            try:
                o, e = p.communicate(timeout=timeout)
                out.append(CLIResult(rc=p.returncode, out=o, err=e))
            except subprocess.TimeoutExpired:
                p.kill()
                o, e = p.communicate()
                out.append(CLIResult(rc=124, out=o, err=e + b"\n[TIMEOUT]"))
        return out


@pytest.fixture
def fs(tmp_path) -> FS:
    return FS(tmp_path)


@pytest.fixture
def mod(tmp_path, monkeypatch):
    """把 fstate.py 作为一个独立模块加载，用于白盒测纯函数/内部不变量。

    加载后把 CACHE_HOME 指到本测试的临时目录，避免污染真实 ~/.dsh。
    """
    import importlib.util

    monkeypatch.setenv("FSTATE_HOME", str(tmp_path / "cache"))
    name = f"fstate_under_test_{next(_MOD_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, FSTATE)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    # @dataclass 在 exec_module 时会通过 sys.modules[cls.__module__] 查字符串注解
    # （因为源文件用了 from __future__ import annotations）。不先注册进去会报
    # AttributeError: 'NoneType' object has no attribute '__dict__'。用唯一名字避免
    # 同一进程里多个 fixture 实例互相覆盖 sys.modules 条目。
    sys.modules[name] = m
    spec.loader.exec_module(m)
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    m.CACHE_HOME = cache
    return m
