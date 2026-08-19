"""LinkedIn provider variant for Company Page posting.

Lists organizations the authenticated member administers, lets the user
pick one, and publishes to that Company Page via the organization URN.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .exceptions import APIError
from .linkedin import API_BASE, LINKEDIN_HEADERS, LinkedInProvider
from .types import AccountProfile, CommentResult, InboxMessage

logger = logging.getLogger(__name__)


class LinkedInCompanyProvider(LinkedInProvider):
    """LinkedIn provider scoped to Company Page posting."""

    @property
    def platform_name(self) -> str:
        return "LinkedIn (Company Page)"

    @property
    def required_scopes(self) -> list[str]:
        return [
            "r_basicprofile",
            "w_member_social",
            "w_organization_social",
            "r_organization_social",
            "rw_organization_admin",
        ]

    def get_user_pages(self, access_token: str) -> list[dict]:
        resp = self._request(
            "GET",
            f"{API_BASE}/v2/organizationalEntityAcls"
            "?q=roleAssignee&role=ADMINISTRATOR"
            "&projection=(elements*(organizationalTarget~(id,localizedName,vanityName,logoV2(original~:playableStreams))))",
            access_token=access_token,
            headers=LINKEDIN_HEADERS,
        )
        data = resp.json()
        pages: list[dict] = []
        for element in data.get("elements", []):
            org = element.get("organizationalTarget~", {})
            org_urn = element.get("organizationalTarget", "")
            org_id = org_urn.split(":")[-1] if org_urn else org.get("id", "")
            logo_url = None
            logo = org.get("logoV2", {}).get("original~", {})
            elements = logo.get("elements", [])
            if elements:
                identifiers = elements[0].get("identifiers", [])
                if identifiers:
                    logo_url = identifiers[0].get("identifier")
            pages.append(
                {
                    "id": str(org_id),
                    "name": org.get("localizedName", ""),
                    "handle": org.get("vanityName", ""),
                    "access_token": access_token,
                    "picture": logo_url,
                }
            )
        return pages

    def get_organization_profile(self, access_token: str, organization_id: str) -> AccountProfile:
        """Fetch identity fields for one selected Company Page.

        Company tokens belong to the administering member, so the inherited
        ``get_profile`` method resolves ``/v2/me`` and must not be used to
        refresh a stored organization account.
        """
        organization_id = str(organization_id)
        for page in self.get_user_pages(access_token):
            if str(page["id"]) == organization_id:
                return AccountProfile(
                    platform_id=organization_id,
                    name=page["name"],
                    handle=page.get("handle"),
                    avatar_url=page.get("picture"),
                    follower_count=page.get("followers_count", 0),
                    extra=page,
                )

        raise APIError(
            "The selected LinkedIn Company Page is no longer available to this account.",
            platform=self.platform_name,
            retryable=False,
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Comment on a post as the selected Company Page."""
        organization_id = self.credentials.get("organization_id")
        if not organization_id:
            raise APIError(
                "LinkedIn Company comments require the selected organization ID.",
                platform=self.platform_name,
                retryable=False,
            )

        actor = f"urn:li:organization:{organization_id}"
        return self._publish_comment_as(access_token, post_id, text, actor)

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[InboxMessage]:
        """Defer Company Page inbox polling until it can use an organization author.

        The base LinkedIn implementation derives a member URN from ``/v2/me``.
        Reusing it for a Company Page makes LinkedIn reject every poll and burns
        the shared application quota.  ``InboxSyncEngine`` treats
        ``NotImplementedError`` as an unsupported provider surface and skips it.
        """
        raise NotImplementedError("LinkedIn Company inbox polling requires an organization author")
