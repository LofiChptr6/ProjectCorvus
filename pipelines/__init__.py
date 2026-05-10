"""Python-driven sector skill pipeline.

Replaces the Claude Code harness path (`claude -p "/atlas-respond"`) with a
deterministic flow: bundler → template render → vLLM tool-loop → DB writes.

Entrypoint: `pipelines.runner.run_skill(agent_name, skill_type)`.
"""
from pipelines.runner import run_skill  # re-export

__all__ = ["run_skill"]
