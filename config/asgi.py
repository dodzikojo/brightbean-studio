import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

django_application = get_asgi_application()

from apps.mcp.routing import create_application  # noqa: E402

application = create_application(django_application=django_application)
