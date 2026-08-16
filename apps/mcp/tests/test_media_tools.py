from __future__ import annotations

import pytest

from apps.mcp.tests.test_content_tools import _call, _oauth_principal, _user, _workspace


@pytest.mark.django_db
def test_search_media_cursor_pages_are_stable_and_non_overlapping(django_user_model):
    from apps.media_library.models import MediaAsset

    user = _user(django_user_model, "media-pages@example.com")
    workspace = _workspace("Media pages")
    principal = _oauth_principal(user, workspace)
    for index in range(3):
        MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_by=user,
            file=f"media/test-{index}.png",
            filename=f"test-{index}.png",
            media_type="image",
            mime_type="image/png",
            processing_status="completed",
        )

    first = _call(principal, "search_media", {"workspace_id": str(workspace.id), "limit": 2})
    second = _call(
        principal,
        "search_media",
        {"workspace_id": str(workspace.id), "limit": 2, "cursor": first["next_cursor"]},
    )

    assert first["limit"] == second["limit"] == 2
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert {item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"])
    assert second["next_cursor"] is None
