# Changelog

## 1.0.0 — 2026-11-03

首次发布。

**核心**
- 带磁盘持久状态的文件读取：`read` 在 `--mode auto` 下自己判断——首次给全文、未变给一行摘要、外部改动给 diff。
- `edit` / `write` 改完直接回 unified diff + 精确变更行区间（`SequenceMatcher` 的 opcodes，不含 hunk 上下文），编辑后不必重读全文。
- `explore` 一次成型：目录概览 + 按相关度排序的命中文件 + 命中行 + 函数签名 + 片段 + 下一步命令。
- `digest` 多文件结构总览；`state` / `diff` / `hot` / `reset` 自省与维护。

**健壮性（均为实测踩出来的修复）**
- 首次读取时影子副本写入失败会被静默吞掉，导致该文件第一次外部修改拿不到 diff、只能回全文 —— 现在写前建目录，失败另存 `shadow_error` 而不是无声降级。
- `edit` 的旧片段换行风格不宽容：LF 片段改 CRLF 文件必然失败。现在匹配前按目标文件归一，写回保持原风格。
- `digest` 静默丢弃非源码后缀文件（`.md`、`.txt`、无后缀脚本）。现在明确报告跳过数量，并提供 `--all-ext`。
- `explore` 空关键词会命中所有文件且分数虚高（`"" in text` 恒真）。现在直接报错。
- `read --start/--end` 反向时静默返回空正文，容易被误读成「文件没内容」。现在报错并给出正确用法。

**打包**
- 以 DSH 插件市场要求的形态发布：`package.json` 声明 `dsh.bundle`，根目录带 `cordis.patch.yml`，`lib/index.js` 是零依赖的技能 provider，把包内 `SKILL.md` 挂进 `ctx.skills`。
- 技能正文不复制：候选与正文都在调用时从 `SKILL.md` 现读，改正文不必动 provider，也不会出现两份漂移。
