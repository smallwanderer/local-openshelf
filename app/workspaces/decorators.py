from functools import wraps

from accounts.api_responses import api_error_response

from .models import WorkspaceMembership


def workspace_member_required(function):
    """Require an authenticated user with a resolved active workspace."""

    @wraps(function)
    def wrap(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return api_error_response(
                "AUTHENTICATION_REQUIRED",
                "Authentication is required.",
                status=401,
            )
        if request.workspace is None:
            return api_error_response(
                "WORKSPACE_REQUIRED",
                "No active workspace.",
                status=403,
            )
        return function(request, *args, **kwargs)

    return wrap


def workspace_admin_required(function):
    """Require the active workspace membership to have the admin role."""

    @wraps(function)
    def wrap(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return api_error_response(
                "AUTHENTICATION_REQUIRED",
                "Authentication is required.",
                status=401,
            )
        if request.workspace is None:
            return api_error_response(
                "WORKSPACE_REQUIRED",
                "No active workspace.",
                status=403,
            )
        if request.workspace_membership.role != WorkspaceMembership.ROLE_ADMIN:
            return api_error_response(
                "PERMISSION_DENIED",
                "Workspace admin access is required.",
                status=403,
            )
        return function(request, *args, **kwargs)

    return wrap
