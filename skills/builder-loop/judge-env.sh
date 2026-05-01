# judge agent 凭证 env file（V2.1，本地配置，不进 git）
#
# 路径：~/.claude/skills/builder-loop/judge-env.sh
# 模板：~/.claude/skills/builder-loop/judge-env.sh.example
#
# 加载机制：run-judge-agent.sh phase 0 仅当主 env 缺 ANTHROPIC_API_KEY 时 set -a 模式 source；
#   主会话 Max CC 走 OAuth 不动 ANTHROPIC_API_KEY → judge 独立从这里读凭证不污染主会话。

# 方案 A：copilot-proxy 桥接 GitHub Copilot API
# sk-666 是占位 key（copilot-proxy 不校验内容）；BASE_URL 指向本地代理服务
export ANTHROPIC_API_KEY=sk-666
export ANTHROPIC_BASE_URL=http://localhost:4142
