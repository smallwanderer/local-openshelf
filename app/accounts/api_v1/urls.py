from django.urls import path

from . import views


app_name = "accounts_api"

urlpatterns = [
    path("session/", views.session_bootstrap, name="session"),
    path("login/", views.session_login, name="login"),
    path("logout/", views.session_logout, name="logout"),
    path("tokens/", views.access_tokens, name="tokens"),
    path("tokens/<str:token_type>/<str:identifier>/", views.revoke_access_token, name="token-revoke"),
    path("tokens/<str:token_type>/<str:identifier>/delete/", views.delete_access_token, name="token-delete"),
    path("cli-tokens/", views.cli_tokens, name="cli-tokens"),
    path("cli-tokens/<uuid:uid>/", views.revoke_cli_token, name="cli-token-revoke"),
]
