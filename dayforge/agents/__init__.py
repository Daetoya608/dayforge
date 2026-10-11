"""AgentAdapter и его реализации для Claude Code и Codex (docs/agents.md)."""

from dayforge.agents.base import AgentAdapter
from dayforge.agents.claude_code import ClaudeCodeAdapter
from dayforge.agents.codex import CodexAdapter


def build_adapters(agent_unix_user: str | None) -> dict[str, AgentAdapter]:
    return {
        ClaudeCodeAdapter.name: ClaudeCodeAdapter(agent_unix_user=agent_unix_user),
        CodexAdapter.name: CodexAdapter(agent_unix_user=agent_unix_user),
    }
