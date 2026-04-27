"""Human-readable audit log display."""

from __future__ import annotations

import json
import db.store as store


async def get_session_transcript(session_id: str) -> str:
    row = await store.get_audit_log(session_id)
    if not row:
        return f"Session not found: {session_id}"

    lines = [
        f"=== AUDIT LOG: {session_id} ===",
        f"Agent:    {row['agent_name']}",
        f"Routine:  {row['routine']}",
        f"Started:  {row['created_at']}",
        f"Source:   {row['trigger_source']}",
        f"Duration: {row.get('duration_ms', 0)}ms",
        f"Tokens:   {row.get('prompt_tokens', 0)} prompt + {row.get('completion_tokens', 0)} completion",
        f"Rounds:   {row.get('tool_rounds', 0)}",
        f"Finish:   {row.get('finish_reason', 'unknown')}",
        "",
        "--- SYSTEM PROMPT ---",
        row.get("system_prompt", ""),
        "",
        "--- MESSAGE HISTORY ---",
    ]

    messages = json.loads(row.get("messages", "[]"))
    for msg in messages:
        role = msg.get("role", "?").upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                btype = block.get("type", "?")
                if btype == "text":
                    lines.append(f"[{role}] {block.get('text', '')[:500]}")
                elif btype == "tool_use":
                    lines.append(f"[{role}:TOOL_USE] {block.get('name')} → {json.dumps(block.get('input', {}))[:200]}")
                elif btype == "tool_result":
                    lines.append(f"[{role}:TOOL_RESULT] → {str(block.get('content', ''))[:300]}")
        else:
            lines.append(f"[{role}] {str(content)[:500]}")

    if row.get("final_response"):
        lines.extend(["", "--- FINAL RESPONSE ---", row["final_response"]])
    if row.get("error"):
        lines.extend(["", "--- ERROR ---", row["error"]])

    return "\n".join(lines)
