"""Workspace-safe application services shared by browser, REST, and MCP content flows."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction

from apps.composer.models import Idea, IdeaGroup, PlatformPost, Post, PostMedia, Tag
from apps.social_accounts.models import SocialAccount


def serialize_idea(idea: Idea) -> dict[str, Any]:
    return {
        "id": str(idea.id),
        "workspace_id": str(idea.workspace_id),
        "title": idea.title,
        "description": idea.description,
        "tags": list(idea.tags or []),
        "status": idea.status,
        "group_id": str(idea.group_id) if idea.group_id else None,
        "media_asset_id": str(idea.media_asset_id) if idea.media_asset_id else None,
        "post_id": str(idea.post_id) if idea.post_id else None,
        "created_at": idea.created_at.isoformat(),
        "updated_at": idea.updated_at.isoformat(),
    }


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    for value in tags or ():
        tag = value.strip()
        if tag and tag not in cleaned:
            cleaned.append(tag[:100])
    return cleaned


def _group_for_workspace(workspace, group_id):
    if not group_id:
        return None
    group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
    if group is None:
        raise ValueError("Idea group not found in this workspace.")
    return group


def _sync_tag_records(workspace, tags: Iterable[str]) -> None:
    for name in tags:
        Tag.objects.get_or_create(workspace=workspace, name=name)


def create_idea(
    *,
    workspace,
    author,
    title: str,
    description: str = "",
    tags: Iterable[str] | None = None,
    status: str = Idea.Status.UNASSIGNED,
    group_id=None,
) -> Idea:
    title = title.strip()
    if not title:
        raise ValueError("title is required.")
    if status not in Idea.Status.values:
        raise ValueError("status is invalid.")
    cleaned_tags = _clean_tags(tags)
    with transaction.atomic():
        idea = Idea.objects.create(
            workspace=workspace,
            author=author,
            title=title,
            description=description.strip(),
            tags=cleaned_tags,
            status=status,
            group=_group_for_workspace(workspace, group_id),
        )
        _sync_tag_records(workspace, cleaned_tags)
    return idea


def update_idea(
    idea: Idea,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: Iterable[str] | None = None,
    status: str | None = None,
    group_id: Any = ...,
) -> Idea:
    fields: list[str] = []
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("title cannot be empty.")
        idea.title = title
        fields.append("title")
    if description is not None:
        idea.description = description.strip()
        fields.append("description")
    if tags is not None:
        idea.tags = _clean_tags(tags)
        fields.append("tags")
    if status is not None:
        if status not in Idea.Status.values:
            raise ValueError("status is invalid.")
        idea.status = status
        fields.append("status")
    if group_id is not ...:
        idea.group = _group_for_workspace(idea.workspace, group_id)
        fields.append("group")
    if fields:
        idea.save(update_fields=[*fields, "updated_at"])
        if "tags" in fields:
            _sync_tag_records(idea.workspace, idea.tags)
    return idea


def convert_idea_to_draft(
    idea: Idea,
    *,
    author,
    social_accounts: Iterable[SocialAccount],
    allow_reconvert: bool = False,
) -> Post:
    accounts = list(social_accounts)
    if any(account.workspace_id != idea.workspace_id for account in accounts):
        raise ValueError("All social accounts must belong to the idea workspace.")

    with transaction.atomic():
        locked_idea = Idea.objects.select_for_update().get(id=idea.id, workspace_id=idea.workspace_id)
        if locked_idea.post_id and not allow_reconvert:
            raise ValueError("This idea has already been converted to a post.")
        media_ids = list(locked_idea.media_attachments.values_list("media_asset_id", flat=True))
        if not media_ids and locked_idea.media_asset_id:
            media_ids = [locked_idea.media_asset_id]
        post = Post.objects.create(
            workspace=locked_idea.workspace,
            author=author,
            title=locked_idea.title,
            caption=locked_idea.description,
            tags=_clean_tags(locked_idea.tags),
        )
        PlatformPost.objects.bulk_create([PlatformPost(post=post, social_account=account) for account in accounts])
        PostMedia.objects.bulk_create(
            [PostMedia(post=post, media_asset_id=media_id, position=index) for index, media_id in enumerate(media_ids)]
        )
        locked_idea.post = post
        locked_idea.save(update_fields=["post", "updated_at"])
    return post


def update_draft_fields(
    post: Post,
    *,
    title: str | None = None,
    caption: str | None = None,
    first_comment: str | None = None,
    internal_notes: str | None = None,
    tags: Iterable[str] | None = None,
) -> Post:
    if not post.is_editable:
        raise ValueError("This post is not editable.")
    values = {
        "title": title,
        "caption": caption,
        "first_comment": first_comment,
        "internal_notes": internal_notes,
    }
    fields: list[str] = []
    for field, value in values.items():
        if value is not None:
            setattr(post, field, value)
            fields.append(field)
    if tags is not None:
        post.tags = _clean_tags(tags)
        fields.append("tags")
    if fields:
        post.save(update_fields=[*fields, "updated_at"])
        if "tags" in fields:
            _sync_tag_records(post.workspace, post.tags)
    return post
