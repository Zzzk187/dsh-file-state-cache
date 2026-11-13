# 开发记录 / 交接手册

> 写于 **2026-11-13**，截至 **v1.1.1（GitHub Release + npm 首发均已落地）**。
> 面向：以后大更新时接手的 Agent / 开发者。
> 目标：**不用翻聊天记录就能安全继续**。本文记录「做了什么、为什么这么做、哪些坑别再踩、下次怎么开工」。
> 版本逐条历史看 [CHANGELOG.md](CHANGELOG.md)，完整用法与实测数据看 [SKILL.md](SKILL.md)，用户向说明看 [README.md](README.md)。

---

## 0. 一句话现状

- 功能开发**已收口**，进入「验收 + 常态化维护」。不建后台服务、不建计划任务、不建数据库。
- 工作树干净；本地 `main` = `origin/main`；`v1.1.0` / `v1.1.1` 两个轻量 tag 与 GitHub Release 齐备。
- **npm 首发成功**：`dsh-file-state-cache@1.1.1`（public），`dist-tags.latest=1.1.1`，线上 tarball shasum 与本地 `npm pack --dry-run` 逐字一致（第 9 节）。
- 双版本全量回归 **94 passed**（Python 3.8.20 与 3.13.13，Windows）。
- **正确性侧已无已知待修 bug**；剩下的都是主动取舍（见第 10 节），不是缺陷。

---

## 1. 项目定位与不可动摇的红线

**定位**：轻量级 AI Agent 文件状态缓存工具 —— 减少重复读取、准确识别变化、用 Diff 减少重复输出、保证读写可靠、降低 Token 消耗。

**目标平台**：Windows 10 / 11，**Python 3.8+**，**零第三方运行时依赖**（pytest 仅测试期），保持 DSH 插件兼容。**Linux / macOS 明确 out of scope，不做跨平台适配**。

**必须坚持**：轻量、可靠、快速、简单。禁止为加功能引入复杂架构。

**禁止**（历轮反复确认）：
- 加新业务功能、数据库、向量模型、复杂配置系统；
- 加后台服务 / 常驻进程 / 计划任务；
- 大规模重构；
- 改现有 CLI 命令与参数的使用方式；
- 破坏 DSH 插件安装与调用方式；
- 引入第三方**运行时**依赖。

**两条铁律**（m00001 定下，所有修复都以此为准）：
1. 任何无法确认的状态，**必须降级为重新读取，绝不伪造缓存命中**。
2. 宁可少缓存一次，也不能错误地告诉 Agent 文件没有变化；宁可重新读取，也不能让 Agent 基于过期内容继续工作。

**兼容性底线**：
- 「未完整交付给 Agent 的文件内容」不得标记为已读；
- 会话凭证不得跨会话冒用；
- 文件变化后不得继续返回旧内容；
- 没有 Shadow 基线必须明确告知用户；
- `edit` 成功必须返回真实 Diff；
- 原始文件编码与换行格式必须保留；
- **默认输出逐字不变**是 CLI 兼容性对比的前提；
- JSON 缓存结构不随意改；确需迁移必须向后兼容（1.1.0 只新增了**可选**字段 `shadow_sha1`，老缓存缺该字段时降级返回 `None`）。

---

## 2. 环境事实（实跑确认，非推断）

| 项 | 值 |
|---|---|
| 仓库根 | `C:\Users\xing\Documents\deepseek-harness\default-workspace\dsh-file-state-cache` |
| 远程 | `https://github.com/Zzzk187/dsh-file-state-cache.git`（branch `main`） |
| 最新 Release | `https://github.com/Zzzk187/dsh-file-state-cache/releases/tag/v1.1.1`（id `444282778`，draft=false） |
| npm 包 | `dsh-file-state-cache@1.1.1`，https://www.npmjs.com/package/dsh-file-state-cache （2026-11-13 首发，public，官方 registry） |
| 实测系统 | Windows 10 Pro，NT `10.0.26300` |
| 默认 `python` | Python **3.13.13** + pytest **9.2.0** |
| Py3.8 解释器 | `D:\Software\Dev\Miniforge\envs\fstate_py38\python.exe`（**3.8.20** + pytest **8.3.4**） |
| 安装命令 | `dsh plugin --profile web add Zzzk187/dsh-file-state-cache` |
| 语法核验 | `ast.parse(src, feature_version=(3,8))`；顶层 import 全 stdlib |
| 行尾 | 全仓库纯 LF（`*.pyc` / `__pycache__/` / `.pytest_cache/` 已忽略）；`.gitattributes` 强制 `scripts/fstate` 为 LF（Windows checkout 转 CRLF 会让 shebang 报 `bad interpreter`） |

**性能评估资产在仓库外**：`C:\Users\xing\Documents\deepseek-harness\default-workspace\perf-eval\`（`measure.py` / `native.py` / `analyze.py` / `components.py` / `measure_brief.py` / `verify_report.py` / `REPORT.md` / `report_data.json` / `tables.txt` / `brief_recheck.*` / `raw/` 90 份 gzip 完整输出 + sha256）。**不在 git 里，不随仓库分发**；换机器需按 `REPORT.md` 重新生成。上一轮用户明确要求「全部保留不动」，别删。

---

## 3. 版本编年史

| 提交 | 版本 / tag | 日期 | 做了什么 | 为什么 |
|---|---|---|---|---|
| `476bc98` 及之前 | 1.0.x ~ 1.0.9 | ~2026-11-04 | 原始功能 `explore/read/edit/write/digest/state/diff/reset/hot` | 项目起点。1.0.9 的教训：「1.0.8 前自测全绿是测试与实现同一盲点」 |
| `1eeeaf2` | **v1.1.0** | 2026-11-11 | 质量优化：P0 缓存正确性 / 并发安全 / 文件完整性，P1 异常恢复 / pytest 回归；发布 Release `441910572` | 首轮 m00001 要求「只做现有功能质量优化、不加业务功能」。用**两份独立 subagent 审计交叉验证**（避免同源盲点），先写复现测试再修 |
| — | （继续轮） | 2026-11-11 | 收掉 3 个残留 bug：`--min-score` nan/inf、stdin 非法字节静默替换、iter_files 静默剪枝；73→83 用例 | m00609「继续啊」。真 Linux 内核本机做不到（无发行版/Docker），如实标「未验证」并改用 POSIX 分支白盒测试固化 |
| `3cbec4a` | **v1.1.1** | 2026-11-13 | 使用侧优化：文档「值得/不值得」清单 + 大文件全文提示 + `explore/digest --brief` 精简档；发布 Release `444282778` | m01809「还能优化吗」→ 用户选 **"1、2"**（L0 文档 + L1 低风险代码）。性能评估发现 digest 大目录 Token 倒退 +114%，用 opt-in 开关修到 −15% |
| `4445061` | （docs，无新 tag） | 2026-11-13 | 清 3 个测试缓存目录；README.md / README.en.md 各 236→139 行通俗化；不改版本号 | m02199「清理不要的东西，保持结构的整洁性，同时 README 要简单通俗易懂」。用户确认只清缓存、`perf-eval/` 保留；授权「提交并推送」 |
| `9790f70` | （docs，无新 tag） | 2026-11-13 | 新增 `DEVLOG.md` 开发记录与交接手册（14 节）；README 中英「更多」节加入口指针；不改版本号、不发 Release | m02285「写个完整的开发记录，以后大更新时再用到你」。纯文档、提交并推送留档 |
| — | （npm 首发，非 git 提交） | 2026-11-13 | `dsh-file-state-cache@1.1.1` 首次发布到 npm（public）；不改代码 / 版本号 / tag / Release | m02288「怎么发布到 npm」。账号 `zzzk187` 开 2FA `auth-and-writes`，`npm publish` 直跑报 `EOTP`，用户自己终端带 `--otp` 发成功；第 9 节记全渠道与坑 |

**发布渠道有两条、互相独立**：① GitHub（tag + Release）② npm（`npm publish`）。**发 GitHub ≠ 发 npm**，下次大更新两条都要各发一遍；npm 渠道 2026-11-13 才首次补上，之前各轮只发了 GitHub。GitHub 侧：轻量 tag（与 v1.0.x 一致）+ GitHub API 建 Release；v1.1.0 走 Clash 代理（直连超时），v1.1.1 与 `4445061` **直连成功、未动系统代理**。npm 侧流程与 2FA / staged 坑见第 9 节。

---

## 4. 1.1.0 修过的真 bug（「为什么代码长这样」）

> 基线：先写 pytest 复现，跑出 **18 failed / 39 passed / 5 errors**，再按 P0→P1 修。这是本项目固定节奏，见第 12 节。

**P0 · 缓存正确性**
- **头号问题：跨会话伪造缓存命中**。`_bump` 每次读都无条件刷新 `session`，且非全文读不清 `full_version` → 新会话 B 做一次部分读后就能拿到「内容未变（sha1 相同）」，跳过从未进上下文的正文。修法：凭证与**签发会话共同签发**（`signing_session`），只有 `delivered_full` 才动 `session` / `full_version`；非全文读两项都不动。这正是铁律 1 的落点。
- Shadow 同行数篡改编造假 diff → 新增 `shadow_sha1` 内容校验；老缓存缺字段**降级返回 `None`**（向后兼容）。
- sha1 与正文 TOCTOU（两次读盘之间文件变了）→ `_read_once` + `_decode_bytes` **同源 sha1**。
- 降级全文只给 400 行、末尾空行 shadow round-trip 丢行、`_key` 无条件 `.lower()`（Windows 大小写不敏感但路径语义更该用 `os.path.normcase`）→ 逐一修正。
- 二进制判定不足：`latin-1` 万能兜底让二进制蒙混过关 → 新增 `_looks_binary`（NUL 或控制字符占比过高）。

**P0 · 并发安全**
- `edit` 无锁导致 lost update → `_cache_lock`：**Windows `msvcrt.locking` LK_NBLCK / POSIX `fcntl.flock`**，进程崩溃由 OS 自动释放；不引入 Redis / 数据库。
- `_save_meta` / `save_shadow` 非原子 → `_atomic_cache_write`：唯一 tmp + fsync + `os.replace`，失败重试 6 次。
- spill 临时文件无 pid 会互相覆盖 → `_unique_spill` 用 pid + `O_EXCL`。
- 硬链接用 `r+b` 就地写失败无回滚 → 写前备份 + 失败 `_rollback`。
- `write` 自检失败时**已覆盖原文件却报告没改**、>8MB 的 write 破坏原文件 → 写前拒 + 写后字节自检 + 回滚。

**P1 · 异常恢复 / 完整性**
- 坏 JSON 静默吞掉 → 自愈；`cmd_hot` 计数告警；`cmd_reset` / `main` 异常兜底；`_SpillError` / `_StdinDecodeError` 明确报错，**不静默吞异常**。
- UTF-8 / UTF-8 BOM / GBK、CRLF / LF、空文件、末尾换行、二进制拒、大文件限 —— 全部保持原样。
- 继续轮：`--min-score` 用 `_finite_float`（`math.isfinite`）拒 nan/inf；`_read_stdin_text` 末行 `errors="replace"` 会静默写 U+FFFD → 抛 `_StdinDecodeError`、main 专门分支 rc=2「文件没被改动」；`iter_files` 静默剪枝 → info 记 4 类计数 + `_pruned_note(info)`，**无剪枝返回空串**（不加噪音以保 CLI 逐字兼容）。

**继续轮与 Windows 收尾轮补的测试**：POSIX 锁分支白盒固化（注入假 fcntl + 代理 `os.name="posix"`）；`tests/test_windows_safety.py` 10 个用例用真实 `ctypes.WinDLL("kernel32")` + `CreateFileW` 独占 / FILE_SHARE_READ 句柄，覆盖 12 进程并发读、8 进程并发 edit 全保留、外部修改检测、占用时 read OK 但 write rc2 且原文件保住、只读触发 `os.replace` PermissionError（`winerror=5`）六次重试、持锁进程 `os._exit(7)` 崩溃后无死锁、陈旧 tmp 清理、坏 JSON 并发自愈、Shadow 丢失降级全文 501 行、同行数篡改拒绝。

**注意**：`\x81w` 是**合法 GBK 双字节**，测非法字节要用 `\x80`。

---

## 5. 架构关键点（改代码前必读）

- **凭证签发**：`_bump` 只在 `delivered_full` 时签 `full_version` + `session`；局部读 / 截断 / diff **都不签**。跨会话（`DSH_SESSION_ID` 变了）必重新交付全文。压缩但 ID 不变 → 凭证**不作废**（已知限制）。
- **JSON 结构**：唯一新增可选字段 `shadow_sha1`；读取老缓存缺字段时降级，绝不假装命中。
- **`_cache_lock`**：Windows `msvcrt.locking` / POSIX `fcntl.flock`；崩溃由 OS 释放，不留死锁。
- **`_atomic_cache_write`**：唯一 tmp + fsync + `os.replace` 重试 6 次；Windows 文件替换失败有安全处理。
- **同源 sha1**：`_read_once` 读一次盘，`_decode_bytes` 解码，hash 与正文来自同一份字节，杜绝 TOCTOU。
- **`_looks_binary` / 硬链接回滚 / `_unique_spill` / `_rollback`**：见第 4 节。
- **`_emit(text, max_chars, kind)`**：`len(text) <= max_chars` 直接 print 返回 `None`；超长落 `CACHE_HOME/spill/` 并只给头 60% + 尾 30% 预览。`read --max-chars` **默认 20000**，`--mode full` 不受 400 行限制但**仍受 `--max-chars` 限制** → 大文件要完整正文必须显式放大。
- **`--brief` 门控**：`brief = bool(getattr(args, "brief", False))`，所有新行为都门控；**不带开关时输出与 1.1.0 逐字一致**。开启后 explore 砍「目录/后缀」概览、`签名`、`片段`、edit 建议行；digest 每文件只留首条签名。**保留**：header、扫描行、`### rel:行 score ★定义`、`命中`、`下一步 read`、以及**全部 ⚠ 剪枝/截断/跳过警告**。「砍了什么要明说」——精简说明行只在真的省略内容时才打印。
- **安全根因**：`explore` / `digest` 命中数恒为 0、**不产生 read 免读凭证**，所以精简它们的输出永远不会触发「未完整交付却标已读」的 P0 红线。护栏测试 `test_brief_does_not_create_read_credential` 固化了这一点。
- **常量**：`SHADOW_LIMIT = 2*1024*1024`、`SKIP_SCAN_LIMIT = 2*1024*1024`、`DEFAULT_MAX_LINES = 400`、文件 >8MB 拒。

---

## 6. 代码地图（截至 v1.1.1；行号会漂移，以 `grep -n "def xxx" scripts/fstate.py` 为准）

| 函数 / 常量 | 大致位置 | 作用 |
|---|---|---|
| `SKIP_DIRS` / `SKIP_EXT` / `DIGEST_EXT` / `SKIP_NAME_RE` | 168–206 | 扫描白/黑名单、`SIG_RE` / `DEF_RE` |
| `_session_id` / `_key` / `def_boost` / `def_line` | 218–281 | 会话标识、缓存键（`os.path.normcase`）、定义评分 |
| 常量三件套 | 209–213 | `SHADOW_LIMIT` / `SKIP_SCAN_LIMIT` / `DEFAULT_MAX_LINES` |
| `_emit` | ~944 | 输出 + spill 预览 |
| `cmd_read` | ~1041 | 六态 `auto/full/range/diff/changed/cached`；命中时 `return 0` 不写盘 |
| `cmd_edit` / `cmd_write` | ~1366 起 | 改 + 真实 Diff + 写后自检与回滚 |
| `cmd_digest` | ~1829 起 | 结构摘要；1.1.1 加 `brief` / `trimmed` |
| `cmd_state` | ~1940 | 纯 JSON 状态 |
| `iter_files` / `_pruned_note` | ~2132 / ~2169 | 扫描 + 剪枝可观测（无剪枝返回空串） |
| `cmd_explore` | ~2194 起 | 探索；1.1.1 加 `brief` 门控 |
| `main` / CLI parser | ~2381 / 2392–2480 | `allow_abbrev=False`；`explore` / `digest` 各带 `--brief` |

`lib/index.js`（215 行）是纯 DSH skill provider，只把 `SKILL.md` 挂到 `ctx.skills`（`BUNDLED_SKILL_RANK=600`、watch 带 150ms debounce），**不解析任何 CLI 参数、不碰 fstate.py 输出** → 改 `scripts/fstate.py` 不影响插件接线。`scripts/fstate`（59 行 bash launcher）依次试 `$FSTATE_PYTHON` → `python3`/`python` → Windows 常见路径，找不到报 `ERROR: no Python 3.8+ found.`（exit 127）。`package.json` 的 `files` **不含 `tests/`**。

---

## 7. 测试体系

**设施**：`tests/conftest.py` 的 `FS` fixture（每用例独立 `work/` + `cache/`、真子进程跑 CLI、env 隔离、`newline=""` 保真；辅助方法 `meta()` / `set_shadow()` / `drop_shadow()` / `make_hardlink()` / `run_parallel()` 等）；`mod` fixture 白盒把 `fstate.py` 加载为唯一名独立模块（**先 `sys.modules[name]=m` 再 `exec_module`**，绕开 dataclass 字符串注解报错），并把 `CACHE_HOME` 指到临时目录。

**7 个测试文件**：`test_cache_correctness.py` / `test_concurrency.py` / `test_exception_recovery.py` / `test_file_integrity.py` / `test_hardening_continue.py` / `test_windows_safety.py`（`pytestmark` skipif `os.name != "nt"`）/ `test_brief_output.py`（1.1.1 新增 11 用例）。共 **94 用例**（1.1.0 时 83）。

**必跑命令**：
```bash
python -m pytest tests/ -q
D:\Software\Dev\Miniforge\envs\fstate_py38\python.exe -m pytest tests/ -q
```
不产生缓存的跑法（清理后复核用）：`python -B -m pytest tests/ -q -p no:cacheprovider`（+ `$env:PYTHONDONTWRITEBYTECODE='1'`）。

**CLI 逐字兼容对比**：把旧版源码按字节导出后，用两个同构独立沙箱跑相同序列，归一化绝对路径与脚本路径，逐步比对 `(rc, stdout, stderr)`。1.1.0→1.1.1 实跑 **21 序列 / 23 步全 [OK]**，且旧版拒绝 `--brief`(rc=2)、新版接受(rc=0)。`tests/test_hardening_continue.py::test_no_prune_no_extra_line` 断言默认无剪枝时输出不含「剪枝」——这是「精简档必须默认关闭」的硬约束。

**性能复测**：`perf-eval/measure.py`（90 unit）+ `perf-eval/measure_brief.py`（A=原生 / Bd=fstate 默认 / Bb=fstate `--brief`，三档 × warmup1 + 5 repeats 取中位数），原始完整 stdout 落 `raw/*.json.gz` 留证；`perf-eval/verify_report.py` 11 项关键数字与 `report_data.json` 逐项核对 → `VERIFY_REPORT_OK / TOTAL 11 FAIL 0`。

---

## 8. 性能评估结论（不要粉饰）

> 来源：`perf-eval/REPORT.md`（9 节完整报告 + 可复核性 + 未测量项）。**90 unit 全部 `rc_bad=0` / 无截断 / 全 rc=0**。Token 用 **tiktoken 0.14.0 `cl100k_base` 真实 BPE** 计数，但已如实标注「**不是目标模型（hy4free / DeepSeek）专属计费词表**」；组件毫秒与端到端 wall 分开标注、不互充。

**核心结论：省的是 Token，不是时间。**

- **真正省**：同会话反复读中/大文件（读 10 次 large −88.7% / medium −84.4%；读 5 次 −78.8% / −74.9%）、中/大文件 edit 确认（−45.9~−49.0%）、接受只看差异的外部改动（−45.5~−48.9%）、同会话 explore 后反复读（n=5 省 59.9~71.7%）。
- **一律不省甚至反费**：首读（≈0% Token 却慢 81~100%）、**跨会话**（S2b 护栏 ≈0%）、**强制全文**（S3b 护栏 ≈0%）、小文件（读 10 次仅 −27.9%、edit 仅 −5.9%、一次性 explore+read **+67.7%**）、digest 中/大目录（**+109~114%**）、一次性 explore+read（n=1 +10.7~+67.7%）、低延迟少调用场景。
- **全场景 B 慢 75~113%**：B 每次 CLI ≈100~110ms、A ≈55~62ms。瓶颈 = `python -c pass` 21.2ms + import `fstate.py`(约 129KB) 63.7ms；**cached 纯命中不写盘仍要 85.0ms**；首读另有两次 fsync 原子写（`_save_meta` ≈4.7ms + `save_shadow` 2.86~4.64ms）。读盘与 sha1 其实是 µs 级。
- **护栏证明省 Token 不靠藏信息**：跨会话与强制全文两个对照场景 Token 差都回到 ≈0%、命中数 0；首读 A/B 字符逐字等价（small 462 / medium 4368 / large 18587）。B 全文步骤显式 `--mode full --max-chars 200000`，杜绝用默认截断制造假节省。
- **1.1.1 `--brief` 复测**：`T_digest` medium **+118.4% → −3.1%**、large **+114.4% → −15.0%**（收窄 121~129pp），重复 3 次 medium +113.6% → −5.3%、large +119.3% → −13.1%；`T_explore` small +65.8% → +52.4%、medium +24.0% → +19.2%、large +10.9% → +8.6%。**诚实口径**：`--brief` 省 Token 的机制确实是减少输出，但减的是非必要装饰、用户主动 opt-in、默认一字不变、定位信息与全部 ⚠ 保留 —— 不得包装成「不减信息的纯优化」。

**未测量 / 未验证项**：目标模型专属计费 tokenizer 的绝对费用、真实 Agent 端到端（含推理）耗时；Linux / macOS 是 **out of scope**，不是「未验证缺陷」。

---

## 9. 发布流程（下次怎么发）

1. 发布前：`python -m pytest tests/ -q`（双版本各一遍）、`git status --short`、`git diff --stat`。
2. 本地提交 → **先试直连**：`git ls-remote origin HEAD` 或直连 `git push origin main`。v1.1.1 与 `4445061` 均直连成功。
3. 仅当直连失败（超时）才按 `clash-verge-proxy` 技能临时设**进程级** `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:7897`；不改全局配置、不写系统代理注册表。注意 `api.github.com` 返回 `403 rate limit exceeded` 是 **REST API 限流、不是连不上**，`git push` 走另一套端点不受影响，别因此误开代理。
4. `git tag v1.x.x`（**轻量 tag**，与 v1.0.x 一致）+ `git push origin v1.x.x`。
5. Release：用 `git credential fill` 在**内存**取 GCM `gho_` token（只打印前缀、不打印值），走 GitHub API `POST /repos/Zzzk187/dsh-file-state-cache/releases`，`draft=false`、`target_commitish=main`。本环境未装 `gh`。
6. 复核：`git rev-parse HEAD` / `origin/main` / `v1.x.x` / Release `target_commitish` **四者 commit 一致**，工作树干净。

### npm 渠道（与 GitHub 独立，别漏发）

`package.json` 已发布就绪：`name=dsh-file-state-cache`、`publishConfig.access=public`、`registry=https://registry.npmjs.org/`、`files` 白名单 10 项（`lib/` `scripts/` `SKILL.md` `README.md` `README.en.md` `CHANGELOG.md` `cordis.patch.yml` `LICENSE`）。**发 npm 不改变 DSH 安装方式**（仍是 `dsh plugin --profile web add Zzzk187/dsh-file-state-cache` 走 GitHub 源）；`files` 不含 `DEVLOG.md` / `tests/`，npmjs 渲染 README 时相对链接指回 GitHub 不会坏。

1. 登录：`npm login`，`npm whoami` 应回 `zzzk187`。
2. 干跑：`npm publish --dry-run`（等价 `npm pack --dry-run`），确认 10 文件 / 96.2 kB / shasum。
3. **版本号不可复用**：先 `npm version patch|minor|major` 再发；已发过的版本改不动，只能发新版本。
4. 发布：本账号 `zzzk187` 开 **2FA `auth-and-writes`**（写操作也要二次验证），`npm publish` 直跑会 `npm error code EOTP` / HTTP 401 `OTP required for authentication`。带一次性密码 `npm publish --otp=123456`（码约 30 秒过期），或点开 npm 12 给的浏览器授权链接认证后重跑。**OTP 是用户私有的，Agent 不代收、不代填**；长期免 OTP 可用 granular automation token（bypass 2FA）写进 `~/.npmrc`。
5. 复核（**一律带 `--prefer-online` 绕过缓存**）：
   - `npm view dsh-file-state-cache version` 与 `dist-tags.latest` → 新版本号；
   - `npm pack dsh-file-state-cache@X.Y.Z --dry-run` 能列出 10 文件；
   - 线上 `dist.shasum` 与发布前本地 `npm pack --dry-run` 的 shasum **逐字一致**（最强证据：线上就是这份代码，没被换过）；
   - 直连 `https://registry.npmjs.org/dsh-file-state-cache/-/dsh-file-state-cache-X.Y.Z.tgz` 带 cache-buster → HTTP 200；
   - `git status --short` 干净、无 `.tgz` 残留、GitHub Release 与 npm 版本号一致。
6. 网页：https://www.npmjs.com/package/dsh-file-state-cache 。

**npm 12 分段发布中间态**（2026-11-13 首发实测）：`npm publish` 先建 `0.0.0-stage` 占位 stub（`description: "Temporary package placeholder for staged publishing"`、`stub: true`、2 文件 / 342 字节），约 98 秒后才 final 出真版本。这期间不带 `--prefer-online` 的 `npm view ... version` 可能回 `0.0.0-stage`、`@1.1.1` 回 `E404 No match found`，是 **CDN / 本地缓存的旧快照**，不代表发布失败。`0.0.0-stage` 会留在 `versions` 列表里、无害；只要 `dist-tags.latest` 指向真版本，`npm install dsh-file-state-cache` 拿到的就是 1.1.1，不用清理。判定成功要看「latest + shasum 一致 + tarball HTTP 200」三者，别只凭一次 `npm view version` 下结论。

**首发实测锚点（1.1.1，用于事后核对传上去的到底是哪份代码）**：线上 `dist.shasum` = `4ea153bd56f3e747fa60135f3b8fcf8a2f75ae16`，与发布前本地 `npm pack --dry-run` 完全相同；`dist.integrity` = `sha512-D8bZqydG1XEBg[...]X3F3b/vR3PQzA==`；包内 10 文件 / 96.2 kB / 解包 245.0 kB。`0.0.0-stage` 创建于 `2026-11-13T14:54:17.741Z`，真版本 `1.1.1` final 于 `2026-11-13T14:55:55.308Z`（约 98 秒中间态）。

**坑**：PowerShell `git show HEAD:scripts/fstate.py | Out-File -Encoding utf8` 会**破坏字节**（中文被重编码 / 加 BOM，129629 → 132156）。导出旧版源码必须用 Python `subprocess.run([...], capture_output=True).stdout` → `Path.write_bytes(blob)` 原样落盘。

---

## 10. 已知限制 / 主动放弃（不是 bug，别当缺陷去"修"）

| 项 | 性质 |
|---|---|
| 一条命令一个进程，cached 命中仍 ~85ms | **架构瓶颈**。根治要常驻 worker = 撞「不增加后台服务」红线。性能上没优化，是权衡后不做 |
| 明文 session 凭证；压缩但 ID 不变 → 凭证不作废 | 设计取舍，已写进文档「拿不准就 `reset`」 |
| 重命名产生缓存孤儿 | 已知限制 |
| `edit` rc=3（片段没找到 / 不唯一） | 已在 README / CHANGELOG 对齐说明 |
| `explore` 定义了 `--max-chars 24000` 但 `cmd_explore` 从不调用 `_emit` | **历史遗留死参数**；改它会改变默认输出、破坏逐字兼容，故 1.1.1 明确不动，记在 CHANGELOG 已知限制 |
| `read --max-chars` 默认 20000 / auto 400 行截断 | 工具会**明说**「已显示前 N/M 行，不算读过」、不签凭证；要全文写 `read FILE --mode full --max-chars 100000` |
| 二进制文件、>8MB 文件拒 | 设计如此，用原生工具 |
| Linux / macOS | Windows-only 定位，out of scope |
| 目标模型专属计费 tokenizer、真实 Agent 端到端耗时 | 未测量，性能报告已标 |

---

## 11. 常态化维护清单（**被动触发，零后台服务**）

1. **改任何东西前后**：跑 `python -m pytest tests/ -q`，Python 3.8 与 3.13 各一遍。
2. **升级 Windows 大版本 / Python / 宿主 DSH 后**：逐个点开插件的**实际功能**复验，别只看「装上了、没报错」——新版宿主 + 旧插件可能静默不生效；改插件源码后必须完全退出重启（含托盘）才生效。
3. **改了行为才同步文档**：[CHANGELOG.md](CHANGELOG.md) + [README.md](README.md) / [README.en.md](README.en.md) / [SKILL.md](SKILL.md) / [package.json](package.json) 版本号一致；测试数量、退出码说明跟着更新。
4. **守住两条底线**：零第三方运行时依赖（pytest 仅测试期）、CLI 参数与默认输出兼容。
5. **缓存不用人工照料**：`reset FILE` 清一个；删 `~/.dsh/fstate-cache/` 清全部（`FSTATE_HOME` 可搬家）；工具自带降级与坏 JSON 自愈。

---

## 12. 什么情况才重新开发 / 大更新开工顺序

**触发条件**（除此之外不主动改代码）：出现真实 bug、上游 DSH 插件接口变动、Windows / Python 新版本不兼容，或用户**明确授权**放宽红线（例如允许常驻进程以优化延迟）。

**开工顺序**（本项目历轮验证有效的固定节奏）：
1. 读本文 → `CHANGELOG.md` → `SKILL.md` → `README.md`，对齐红线。
2. `git status` / `git log` 确认基线；跑双版本全量测试建立绿色基线。
3. **先写 pytest 复现**（基线必须 fail，证明测的是真问题），再动手改。
4. 改动保持最小：`feature_version=(3,8)` 语法核验、零运行时依赖、默认输出逐字不变。
5. 双版本全量回归 → CLI 前后逐字兼容对比（旧版源码按字节导出）→ 性能复测（若影响输出）。
6. 文档 / 版本号 → 交付报告（问题清单 + 严重度 / 修改文件 + 原因 / 新增测试 / 实跑结果 / 未解决问题 / 兼容性风险 / 是否可发布）。**不许虚报测试通过，没实跑的标「未验证」**。
7. 提交 / 推送 / 发 Release 属于对外不可逆动作，**每轮单独确认**，不跨轮延伸授权。

---

## 13. 踩过的坑（跨轮复用，别再踩）

- 改插件源码 ≠ 生效；宿主升大版本后旧插件可能静默跳过，要逐个点开实际功能看一眼。
- PowerShell `Out-File -Encoding utf8` 破坏源码字节（见第 9 节）。
- `\x81w` 是合法 GBK 双字节，测非法字节用 `\x80`。
- 测试 fixture 共享同一 `fs` 时，同名文件会被另一个用例覆盖 → **用例要各自独立**。
- `digest` 小目录每文件只有 1~2 条签名、本没东西可砍，`--brief` 固定打印中文说明行反而倒退 ~28pp → 说明行改为**只在真的省略 ≥2 条或发生截断时才打印**（`if brief and (len(sigs) > len(shown) + 1 or truncated)`）。
- **默认输出逐字不变**是所有 CLI 兼容对比的前提，任何新行为都必须做成默认关闭的可选开关。
- 性能评估别靠减少必要信息制造 Token 节省：B 侧凡要交付整篇一律显式放大 `--max-chars`，并设跨会话 / 强制全文两组护栏。
- 控制台中文乱码只是显示问题（`chcp 65001` + `python -X utf8`），不影响 JSON 数据；别因此误判结果。
- `npm publish` 要过 **2FA `auth-and-writes`**（`EOTP` / HTTP 401），必须 `--otp=xxxxxx` 或走 npm 12 浏览器授权链接；OTP 用户私有，Agent 不代收、不代填。
- npm 12 **staged publishing 中间态**：publish 先建 `0.0.0-stage` 占位、稍后才 final 真版本；这期间不带 `--prefer-online` 的 `npm view` 命中旧快照会误判成「没发上去 / 404」。判定成功看 `dist-tags.latest` + 线上/本地 shasum 一致 + tarball HTTP 200，别只看一次 `npm view version`。

---

## 14. 关键路径速查

| 要看什么 | 去哪 |
|---|---|
| 版本逐条历史、已知限制 | [CHANGELOG.md](CHANGELOG.md) |
| 完整用法、实测数据、踩坑列表（≥#58） | [SKILL.md](SKILL.md) |
| 用户向简明说明 | [README.md](README.md) / [README.en.md](README.en.md) |
| 主实现 | [scripts/fstate.py](scripts/fstate.py) |
| DSH 插件接线 | [lib/index.js](lib/index.js) |
| 测试 | [tests/](tests/) |
| 性能报告（**仓库外**） | `..\perf-eval\REPORT.md` |
| 远程 / Release | https://github.com/Zzzk187/dsh-file-state-cache · https://github.com/Zzzk187/dsh-file-state-cache/releases/tag/v1.1.1 |
| npm 包 | https://www.npmjs.com/package/dsh-file-state-cache （`npm view dsh-file-state-cache version --prefer-online`） |

---

**状态：功能开发已收口，停止开发，等待验收。** 本文即交接点；下次大更新从「第 12 节」开始。
