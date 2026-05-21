"""Auto-discovery loader for per-agent skills.

Phase B of CITATION_ARCH (2026-05-21). Skills are lightweight, agent-callable
analysis utilities. Unlike models (`agents/<name>/models/`), skills do NOT
produce a forecast triple and do NOT write to `agent_forecast`. They return
ONE answer with an `evidence_id` the LLM can attach to a Citation.

Conservative scope: skills are human-authored Python files in a registry.
The LLM picks `from_skill="X"` via the `run_skill` MCP tool but cannot
synthesize new Python on the fly during hourly reviews. Skill authorship
happens via the nightly `*-model-tune` channel (Phase E, deferred).

Each `agents/<name>/skills/<name>.py` must expose:

    SKILL_VERSION     = "0.1.0"
    SKILL_DESCRIPTION = "one-line description for the bundle"

    async def compute(*, agent_name=None, session_id=None, **kwargs) -> dict:
        '''
        Returns:
          {
            "ok": True,
            "result": <any>,        # the answer
            "inputs_used": dict,    # the literal args that produced result
            "evidence_id": int,     # required when ok=True
          }
          or
          {"ok": False, "reason": "..."}
        '''

The skill MUST stamp its own evidence_snapshot row (via db.store.stamp_evidence)
when ok=True, so the citation trail is one-deep. The loader does not stamp
on the skill's behalf — skills know their semantics and what content_hash to
deduplicate on.

The loader mirrors `meta_agent.model_loader` for consistency.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def discover_skills(agent_name: str, agents_root: Path | None = None) -> list[str]:
    """Return sorted list of skill module names in agents/{agent_name}/skills/.
    Validates names against regex; silently skips files that don't match.
    Returns empty list if the agent has no skills dir.
    """
    if not _AGENT_NAME_RE.match(agent_name or ""):
        return []
    root = agents_root if agents_root is not None else Path("agents")
    skill_dir = root / agent_name / "skills"
    if not skill_dir.exists() or not skill_dir.is_dir():
        return []
    out: list[str] = []
    for py in sorted(skill_dir.glob("*.py")):
        name = py.stem
        if name == "__init__":
            continue
        if not _SKILL_NAME_RE.match(name):
            continue
        out.append(name)
    return out


def list_agent_skills(agent_name: str) -> list[dict[str, Any]]:
    """Return per-skill metadata for the review bundle: name, version,
    description. Used by `agent/bundlers/review.py` to advertise the
    skill registry in the prompt."""
    out: list[dict[str, Any]] = []
    for name in discover_skills(agent_name):
        try:
            module = importlib.import_module(f"agents.{agent_name}.skills.{name}")
            module = importlib.reload(module)
        except Exception as exc:
            out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        out.append({
            "name": name,
            "version": str(getattr(module, "SKILL_VERSION", "unset")),
            "description": str(getattr(module, "SKILL_DESCRIPTION", "") or
                               (getattr(module, "__doc__", None) or "").strip().split("\n", 1)[0]),
        })
    return out


async def run_skill(
    agent_name: str,
    skill_name: str,
    args: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Validate skill_name is in `agents/<agent_name>/skills/`, import it,
    call its `compute(**args, agent_name=, session_id=)`, return the result.

    Returns one of:
      {"status": "ok", "payload": {ok: True, result, inputs_used, evidence_id, ...}}
      {"status": "error", "error": "..."}
    """
    if not _AGENT_NAME_RE.match(agent_name or ""):
        return {"status": "error", "error": f"invalid agent_name {agent_name!r}"}
    if not _SKILL_NAME_RE.match(skill_name or ""):
        return {"status": "error", "error": f"invalid skill_name {skill_name!r}"}
    if skill_name not in discover_skills(agent_name):
        return {
            "status": "error",
            "error": (
                f"skill not found: agents/{agent_name}/skills/{skill_name}.py "
                "(skills are registry-only; ad-hoc Python is restricted to nightly model_tune)"
            ),
        }
    try:
        module = importlib.import_module(f"agents.{agent_name}.skills.{skill_name}")
        module = importlib.reload(module)
    except Exception as exc:
        return {"status": "error", "error": f"skill import failed: {type(exc).__name__}: {exc}"}
    if not hasattr(module, "compute"):
        return {"status": "error", "error": "skill lacks compute(**kwargs) entry point"}

    call_args = dict(args or {})
    call_args.setdefault("agent_name", agent_name)
    call_args.setdefault("session_id", session_id)
    try:
        result = await module.compute(**call_args)
    except TypeError as exc:
        return {"status": "error", "error": f"skill called with bad args: {exc}"}
    except Exception as exc:
        return {"status": "error", "error": f"skill crashed: {type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"status": "error", "error": f"skill returned non-dict: {type(result).__name__}"}
    if not result.get("ok"):
        return {"status": "ok", "payload": result}  # graceful decline; surface to caller
    # Sanity-check contract on successful returns
    if "evidence_id" not in result:
        return {
            "status": "error",
            "error": f"skill {skill_name} returned ok=True but no evidence_id (contract violation)",
        }
    return {"status": "ok", "payload": result}
