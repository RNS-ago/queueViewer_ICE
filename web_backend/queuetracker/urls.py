"""Top-level URL routing.

Three functional endpoints, as requested:
  * POST /api/log          — ingest records from the ESP32 logger
  * GET  /                 — public minimal dashboard
  * GET  /advanced/        — advanced dashboard (login required)

Plus Django's auth login/logout and the admin site for convenience.
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from counts import views

urlpatterns = [
    # --- the three requested endpoints ---
    path("api/log", views.api_log, name="api_log"),
    path("", views.public_dashboard, name="public_dashboard"),
    path("advanced/", views.advanced_dashboard, name="advanced_dashboard"),
    path("advanced/keys/", views.manage_keys, name="manage_keys"),
    # --- auth + admin ---
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
]
