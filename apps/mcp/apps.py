import logging

from django.apps import AppConfig

LOG = logging.getLogger(__name__)


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    # ``mcp`` is also the name of a popular PyPI package; pick a unique
    # label so Django's app registry can't collide with one in the future.
    label = "mcp_server"
    verbose_name = "Model Context Protocol Server"

    def ready(self):
        # Force registration of all tools at app boot so `tools/list`
        # returns a complete catalog regardless of which router is hit
        # first. Import-side-effects only.
        from django.db.models.signals import post_migrate

        from apps.mcp import analytics, approvals, calendar, content, context, handlers, inbox  # noqa: F401

        post_migrate.connect(self._register_activity_sweep, sender=self)

    @staticmethod
    def _register_activity_sweep(sender, **kwargs):
        try:
            from background_task.models import Task

            from apps.mcp.tasks import ACTIVITY_SWEEP_INTERVAL_SECONDS, sweep_mcp_activity

            if not Task.objects.filter(verbose_name="sweep_mcp_activity").exists():
                sweep_mcp_activity(
                    repeat=ACTIVITY_SWEEP_INTERVAL_SECONDS,
                    verbose_name="sweep_mcp_activity",
                )
        except Exception:  # noqa: BLE001 - migrations may run before the task table exists.
            LOG.debug("Skipping MCP activity sweep registration (DB not ready)")
