from django.urls import path

from . import views

app_name = "settings_manager"

urlpatterns = [
    path("", views.settings_index, name="index"),
    path("mcp/", views.mcp_overview, name="mcp_overview"),
    path("mcp/activity/", views.mcp_activity, name="mcp_activity"),
    path("mcp/tools/", views.mcp_tools, name="mcp_tools"),
]
