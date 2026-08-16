"""Recurring maintenance for the MCP control plane."""

from __future__ import annotations

import logging

from background_task import background

LOG = logging.getLogger(__name__)
ACTIVITY_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60


@background(schedule=0)
def sweep_mcp_activity():
    from apps.mcp.activity import purge_expired_activity
    from apps.mcp.confirmations import quarantine_stale_external_reservations

    deleted = purge_expired_activity()
    if deleted:
        LOG.info("Swept %d expired MCP activity events", deleted)
    quarantined = quarantine_stale_external_reservations()
    if quarantined:
        LOG.warning("Quarantined %d stale MCP external-action reservations", quarantined)
