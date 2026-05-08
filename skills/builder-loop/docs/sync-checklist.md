# 改动同步 Checklist

> 从 CLAUDE.md §3 外移。本仓 builder commit 后必查。

| 改动类型 | 操作 |
|---------|------|
| 改 `scripts/*.sh` / `agents/*.md` 内容（不增删） | **不操作**（软链已存在，主仓改即时生效）|
| 新增/删除 `scripts/*.sh` 或 `agents/*.md` | `bash install.sh` 创建/移除软链 + 同步 settings.json hook 注册 |
| 改 hook matcher（如 `Read\|Grep` → `Read\|Grep\|WebFetch`）| 直接跑 `bash install.sh` 即可（V2.7.1 起 `find_entry_status()` 三态返回，matcher 字面变化会被识别为 stale 自动删旧加新；输出含「N 条更新」字段）|
| 改 `~/.claude/commands/builder.md` / `planner.md`、`~/.claude/agents/reviewer.md` | 切到 `~/.hongyu.liao_debian12/my-dotfiles` 仓 commit（cc-builder-loop 与 my-dotfiles 是两个独立 git 仓）|
| ✅ **V3.0** dotfiles `~/.claude/commands/builder.md` 同步 | 已落实（dotfiles commit `3349374`）：硬规则按 phase 判定 / 步骤 2 加 V3.0 路径分支 / 老 state 兼容指引 / pause 用法 |
