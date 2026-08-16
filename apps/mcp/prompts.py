"""Read-only prompt templates grounded in authorized BrightBean resources."""

from __future__ import annotations

from typing import Any

from apps.mcp.errors import DomainError
from apps.mcp.resources import _invoke_read_tool

_PROMPTS: dict[str, dict[str, Any]] = {
    "campaign_plan": {
        "description": "Plan a social campaign using workspace brand and calendar context.",
        "arguments": ["workspace_id", "objective"],
        "tool": "get_workspace_context",
        "resource": "context",
        "instruction": "Develop a practical campaign plan for this objective: {objective}.",
    },
    "draft_social_post": {
        "description": "Draft platform-aware social copy using workspace context.",
        "arguments": ["workspace_id", "objective", "platform"],
        "tool": "get_workspace_context",
        "resource": "context",
        "instruction": "Draft social copy for {platform} that supports this objective: {objective}.",
    },
    "weekly_performance_review": {
        "description": "Review measured workspace performance and suggest evidence-based next steps.",
        "arguments": ["workspace_id", "days"],
        "tool": "get_workspace_analytics",
        "resource": "analytics/{days}",
        "instruction": "Review the measured performance for the last {days} days and suggest next steps.",
    },
    "triage_inbox": {
        "description": "Triage the authorized social inbox without sending replies.",
        "arguments": ["workspace_id"],
        "tool": "list_inbox",
        "resource": "inbox/{message_id}",
        "instruction": "Triage inbox items by urgency, sentiment, ownership, and response risk. Do not send replies.",
    },
}


def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "title": name.replace("_", " ").title(),
            "description": definition["description"],
            "arguments": [
                {
                    "name": argument,
                    "required": argument == "workspace_id",
                    "description": "Explicit authorized workspace UUID." if argument == "workspace_id" else None,
                }
                for argument in definition["arguments"]
            ],
        }
        for name, definition in sorted(_PROMPTS.items())
    ]


def get_prompt(principal, request, name: str, arguments: dict[str, str]) -> dict[str, Any]:
    definition = _PROMPTS.get(name)
    if definition is None:
        raise DomainError("resource_not_found", "Unknown BrightBean prompt.")
    workspace_id = arguments.get("workspace_id")
    if not workspace_id:
        raise DomainError("workspace_required", "workspace_id is required for BrightBean prompts.")
    _invoke_read_tool(principal, request, definition["tool"], workspace_id, {})
    values = {
        "objective": arguments.get("objective") or "the stated business goal",
        "platform": arguments.get("platform") or "the most appropriate connected platforms",
        "days": arguments.get("days") or "7",
        "message_id": "{message_id}",
    }
    try:
        instruction = definition["instruction"].format(**values)
        resource_path = definition["resource"].format(**values)
    except (KeyError, ValueError) as exc:
        raise DomainError("invalid_request", "Prompt arguments are invalid.") from exc
    uri = f"brightbean://workspaces/{workspace_id}/{resource_path}"
    return {
        "description": definition["description"],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"{instruction} Use only authorized BrightBean data. Cite the linked resource in the "
                        "analysis, distinguish measured facts from suggestions, and return guidance only."
                    ),
                },
            },
            {
                "role": "user",
                "content": {
                    "type": "resource_link",
                    "uri": uri,
                    "name": f"{name} workspace resource",
                    "mimeType": "application/json",
                },
            },
        ],
    }
