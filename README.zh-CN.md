# dsh-file-state-cache · 文件状态缓存技能

> 让 Agent 的「读文件」变成**有记忆**的动作 —— 读过的文件不再整篇重读。

`ls` 一轮 → `grep` 一轮 → `read` 一轮 → 改完再 `read` 一轮确认。四轮里三轮是在问自己"我刚才是不是看过了？"。本技能把这一串压成一次探索、一个会记住自己发生过的读、以及一个改完直接回 diff 的编辑。

---

## 它到底省什么

| 浪费 | 以前 | 用了之后 |
|---|---|---|
| **探索往返** | `ls`、`grep`、盲目 `read` —— 三轮 | 一次 `explore "关键词"` → 目录概览 + 命中行 + 函数签名 + 代码片段 + 下一条该跑的命令 |
| **重复读未变的文件** | 每次都是全文 | 一行字：*「内容未变，此前已读 N 次，最近读的范围…」* |
| **改完还要验证** | 重读整个文件 | 统一 diff + **精确**的变更行区间 |
| **读 N 个文件才看懂结构** | N 次读 | 一次 `digest` → 一屏 N 个签名 |

状态存在**磁盘上**，不占上下文。这是关键：它能跨工具调用、跨轮次、跨重启活着。普通的一次性工具调用做不到，所以本技能做成「CLI + 技能」，而不是注册一堆工具。

---

## 安装

```bash
dsh plugin --profile web add Zzzk187/dsh-file-state-cache
```

本地路径 / 开发时：

```bash
dsh plugin --profile web add /path/to/dsh-file-state-cache
```

**前提**：Python 3.8+。脚本**零第三方依赖**，只用标准库，不需要 pip install。

装好后技能目录里会出现 `file-state-cache`。

---

## 使用说明

技能加载时，宿主会给出本包的**资源基准目录**；脚本就在它下面的 `scripts/`。手动调用：

```bash
F="<包根目录>/scripts/fstate"

"$F" explore "handleRequest" --root .     # 定位代码：概览 + 命中 + 签名
"$F" read src/app.py                      # 第 1 次：全文。第 2 次：一行字。
"$F" read src/app.py --mode range --start 40 --end 90
"$F" edit src/app.py --old 'return 1' --new 'return 2' --reason "修边界"
"$F" digest src/                          # 一次看整个目录的结构
"$F" state src/app.py                     # 版本 / 行数 / 读过几次 / 是否过期
"$F" hot --n 10                           # 我在反复读哪些文件？（自省）
```

包装脚本找不到 Python 时，自己指一下：

```bash
export FSTATE_PYTHON=/usr/bin/python3
```

Windows 下也可以直接跑：

```powershell
python "<包根>\scripts\fstate.py" explore "关键词"
```

### `read --mode auto`（默认）会替你做判断

| 磁盘状态 | 返回什么 |
|---|---|
| 从没读过 | 全文（超大文件自动分页并给下一页命令） |
| 读过、没变 | **只有一行摘要** —— 不回正文 |
| 读过、磁盘变了 | **只回 diff** + 精确变更行区间 |

### 一次编辑的输出长这样

```
# 已改 src/app.py  → v2  19 行  区间 14..14
--- a/app.py
+++ b/app.py
@@ -11,7 +11,7 @@
-    return {"ok": True, "uptime": 0}
+    return {"ok": True, "uptime": 42}
[确认改动不必重读全文：看局部用 --mode range --start 14 --end 14]
```

**这段 diff 就是验证**。区间 `14..14` 来自 `SequenceMatcher` 的 opcodes，不是 diff 的 hunk 头 —— 所以它不包含那三行上下文，直接指向真正变动的那一行。

---

## 子命令

| 命令 | 作用 |
|---|---|
| `explore QUERY` | 目录概览 + 排序后的命中文件 + 命中行 + 签名 + 片段 + 下一步 |
| `read PATH` | 带缓存读：未变→摘要，变了→diff，`--mode range` 读区间 |
| `edit PATH --old/--new` | 编辑并回 unified diff + 精确变更行区间 |
| `write PATH --content-file` | 整篇写入（新建给预览，覆盖给 diff） |
| `digest PATH...` | 多文件结构总览（行数 + 签名 + 缓存状态） |
| `state PATH` | 版本 / 行数 / 读次数 / 上次编辑区间 / 是否 STALE |
| `diff PATH` | 取最近一次 diff |
| `reset PATH` | 清单个文件缓存 |
| `hot [--n N]` | 读得最多的文件排行 —— 用来照自己有没有反复读 |

`"$F" --help` 看全部参数。

---

## 设计要点

- **磁盘是唯一真相。** 每次读/改前先比 SHA-1。你在编辑器里改过、`git checkout` 过，下次 `read` 立刻发现并给 diff，不会拿旧内容骗人；`state` 会标 `STALE`。
- **影子副本。** ≤2 MB 的文件会留一份 `.shadow`，算 diff 不需要 git。状态文件是 `~/.dsh/fstate-cache/` 下的 `<sha1(path)[:20]>.json`。
- **编码回退。** 源文件按 `utf-8-sig` → `utf-8` → `gbk` → `latin-1` 依次尝试，写回保持原编码。超过 8 MB 或含 NUL 字节的文件直接判「不是文本」，不硬啃。
- **换行宽容。** LF 片段改 CRLF 文件（Windows 很常见）能匹配上，写回仍是 CRLF。
- **输出有上限。** 超长 diff 和日志会落到溢出文件，只回预览 + 路径，不会一次撑爆上下文。
- **缓存位置可搬。** `FSTATE_HOME` 换目录；`reset <path>` 清单个文件；`hot` 看都在攒什么。

## 什么时候**别**用它

- 已经知道确切行号 → 原生读取工具更快，进缓存是负担。
- 一次性脚本、单条命令 → 没有第二遍，缓存零收益。
- 二进制 / 图片 / 超过 8 MB → 它按设计拒绝。

**判断口诀：这件事会发生第二遍吗？会 → 用它；不会 → 用原生工具。**

## 兼容性

在 DSH `0.2.1-rc.5` 上验证通过。技能 provider 走 `ctx.skills.registerProvider`；宿主不传 `control` 对象时会优雅降级（改 SKILL.md 后需重启，而不是热更新）。

注册档位是 rank `600`（bundled）。如果你在 `~/.dsh/dsh-skill/file-state-cache` 也放了一份本地版，**本地版优先** —— 插件不该覆盖你自己的特化版本。

## 许可

MIT
