# 改动同步 Checklist

> 从 CLAUDE.md §3 外移。本仓 builder commit 后必查。

| 改动类型 | 操作 |
|---------|------|
| 改 `scripts/*.sh` / `agents/*.md` 内容（不增删） | **不操作**（软链已存在，主仓改即时生效）|
| 新增/删除 `scripts/*.sh` 或 `agents/*.md` | `bash install.sh` 创建/移除软链 + 同步 settings.json hook 注册 |
| 改 hook matcher（如 `Read\|Grep` → `Read\|Grep\|WebFetch`）| 删旧 settings.json 条目后跑 `bash install.sh`（**已知 install.sh `has_entry()` 仅比脚本名不比 matcher，改 matcher 不会更新——见 `.claude/improvements.md`**）|
| 改 `~/.claude/commands/builder.md` / `planner.md`、`~/.claude/agents/reviewer.md` | 切到 `~/.hongyu.liao_debian12/my-dotfiles` 仓 commit（cc-builder-loop 与 my-dotfiles 是两个独立 git 仓）|
