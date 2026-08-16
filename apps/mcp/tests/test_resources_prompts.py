from __future__ import annotations

import json

import pytest
from django.http import HttpRequest

from apps.mcp.tests.test_content_tools import _oauth_principal, _user, _workspace


def _context(principal):
    request = HttpRequest()
    request.user = principal.user
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    return {"principal": principal, "request": request}


@pytest.mark.django_db
def test_resources_list_and_read_authorized_workspace_context(django_user_model):
    from apps.mcp.legacy import METHODS

    user = _user(django_user_model, "resource-context@example.com")
    workspace = _workspace("Resource context")
    workspace.description = "Context only this tenant may read"
    workspace.save(update_fields=["description"])
    principal = _oauth_principal(user, workspace)
    context = _context(principal)

    listed = METHODS["resources/list"]({}, context)
    templates = METHODS["resources/templates/list"]({}, context)
    uri = f"brightbean://workspaces/{workspace.id}/context"
    read = METHODS["resources/read"]({"uri": uri}, context)
    payload = json.loads(read["contents"][0]["text"])

    assert any(resource["uri"] == "brightbean://workspaces" for resource in listed["resources"])
    assert any(template["uriTemplate"].endswith("/context") for template in templates["resourceTemplates"])
    assert payload["id"] == str(workspace.id)
    assert payload["description"] == "Context only this tenant may read"


@pytest.mark.django_db
def test_resource_read_rejects_cross_workspace_access(django_user_model):
    from apps.mcp.legacy import METHODS
    from apps.mcp.protocol import JsonRpcError

    user = _user(django_user_model, "resource-denied@example.com")
    allowed = _workspace("Allowed resource")
    blocked = _workspace("Blocked resource")
    principal = _oauth_principal(user, allowed)

    with pytest.raises(JsonRpcError):
        METHODS["resources/read"](
            {"uri": f"brightbean://workspaces/{blocked.id}/context"},
            _context(principal),
        )


@pytest.mark.django_db
def test_prompts_require_explicit_workspace_and_only_return_guidance(django_user_model):
    from apps.mcp.legacy import METHODS
    from apps.mcp.protocol import JsonRpcError

    user = _user(django_user_model, "prompt-guidance@example.com")
    first = _workspace("Prompt first")
    second = _workspace("Prompt second")
    principal = _oauth_principal(user, first, second)
    context = _context(principal)

    listed = METHODS["prompts/list"]({}, context)
    assert {prompt["name"] for prompt in listed["prompts"]} == {
        "campaign_plan",
        "draft_social_post",
        "triage_inbox",
        "weekly_performance_review",
    }
    with pytest.raises(JsonRpcError):
        METHODS["prompts/get"]({"name": "campaign_plan", "arguments": {}}, context)

    result = METHODS["prompts/get"](
        {
            "name": "campaign_plan",
            "arguments": {"workspace_id": str(first.id), "objective": "Launch"},
        },
        context,
    )
    rendered = str(result)
    assert "Launch" in rendered
    assert f"brightbean://workspaces/{first.id}/context" in rendered
    assert "create_" not in rendered.lower()
    assert "schedule_" not in rendered.lower()


def test_initialize_advertises_resource_and_prompt_capabilities():
    from apps.mcp.legacy import _initialize

    result = _initialize({}, {})
    assert result["capabilities"]["resources"] == {"subscribe": False, "listChanged": False}
    assert result["capabilities"]["prompts"] == {"listChanged": False}
