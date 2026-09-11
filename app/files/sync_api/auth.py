"""API Token authentication for the Sync API.

Validates legacy sync tokens or CLI tokens carrying the ``sync`` scope.
"""

import logging
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone

from accounts.cli_tokens import (
    CLI_SYNC_SCOPE,
    CLI_TOKEN_PREFIX,
    CLITokenAuthenticationError,
    authenticate_cli_token,
)
from accounts.models import APIToken
from workspaces.models import Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)


def api_token_required(view_func):
    """Decorator that authenticates requests via Bearer token.

    On success, ``request.user`` is set to the token owner and
    ``request.api_token`` is set to the APIToken instance.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse(
                {"ok": False, "errors": ["Missing or invalid Authorization header."]},
                status=401,
            )

        key = auth_header[7:].strip()
        if not key:
            return JsonResponse(
                {"ok": False, "errors": ["Empty API token."]},
                status=401,
            )

        if key.startswith(CLI_TOKEN_PREFIX):
            try:
                token = authenticate_cli_token(key, required_scope=CLI_SYNC_SCOPE)
            except CLITokenAuthenticationError as exc:
                return JsonResponse(
                    {"ok": False, "errors": [exc.message], "code": exc.code},
                    status=exc.status,
                )
        else:
            try:
                token = APIToken.objects.select_related("user").get(key=key, is_active=True)
            except APIToken.DoesNotExist:
                return JsonResponse(
                    {"ok": False, "errors": ["Invalid or inactive API token."]},
                    status=401,
                )

        if not token.user.is_active:
            return JsonResponse(
                {"ok": False, "errors": ["User account is inactive."]},
                status=403,
            )

        if isinstance(token, APIToken):
            # Keep the legacy connector token's usage timestamp current.
            APIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        request.user = token.user
        request.api_token = token
        # Sync API requests carry a bearer token, not a session, so
        # ActiveWorkspaceMiddleware (session-based) never runs for them.
        # v1 scopes CLI/sync access to the personal workspace only.
        membership = (
            WorkspaceMembership.objects.filter(
                user=token.user,
                status=WorkspaceMembership.STATUS_ACTIVE,
                workspace__kind=Workspace.KIND_PERSONAL,
            )
            .select_related("workspace")
            .first()
        )
        request.workspace = membership.workspace if membership else None
        request.workspace_membership = membership
        return view_func(request, *args, **kwargs)

    return wrapper
