# 已知环境问题

[保质期: pytest-html 修复或从本机环境移除, owner: hongyu, 正向归宿: requirements-dev.txt / 本机环境]

## pytest-html 插件损坏

本机 pytest-html 插件不可用：跑 pytest（含全量测试与 builder-loop 的 machine / proof）时需加 `-p no:html`，必要时再加 `-p no:cacheprovider`。命令见 `CLAUDE.md` 的 Commands 段。
