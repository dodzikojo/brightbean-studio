import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("oauth2_provider", "0020_cimd_application_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="McpOAuthTokenBinding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token_kind", models.CharField(choices=[("access", "Access token"), ("refresh", "Refresh token")], max_length=10)),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("resource_uri", models.TextField()),
                ("resource_digest", models.CharField(db_index=True, max_length=64)),
                ("granted_scopes", models.JSONField(default=list)),
                ("token_family", models.UUIDField(blank=True, db_index=True, null=True)),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("access_token", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="mcp_binding", to="oauth2_provider.accesstoken")),
                ("application", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="mcp_token_bindings", to="oauth2_provider.application")),
                ("parent_refresh_binding", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="rotated_bindings", to="oauth_server.mcpoauthtokenbinding")),
                ("refresh_token", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="mcp_binding", to="oauth2_provider.refreshtoken")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="mcp_oauth_token_bindings", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(model_name="mcpoauthtokenbinding", index=models.Index(fields=["token_kind", "token_digest"], name="mcp_bind_kind_digest")),
        migrations.AddIndex(model_name="mcpoauthtokenbinding", index=models.Index(fields=["application", "revoked_at"], name="mcp_bind_app_revoked")),
        migrations.AddIndex(model_name="mcpoauthtokenbinding", index=models.Index(fields=["user", "revoked_at"], name="mcp_bind_user_revoked")),
    ]
