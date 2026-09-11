import hashlib
import secrets
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from .models import CLIToken


CLI_TOKEN_PREFIX = "dtr_cli_"
CLI_SYNC_SCOPE = "sync"
CLI_DOCUMENTS_READ_SCOPE = "documents:read"
CLI_DOCUMENTS_WRITE_SCOPE = "documents:write"
CLI_SEARCH_SCOPE = "search"
CLI_RAG_SCOPE = "rag"
CLI_STATUS_SCOPE = "status:read"
CLI_ACCESS_LEVEL_READ_ONLY = "read_only"
CLI_ACCESS_LEVEL_READ_WRITE = "read_write"
CLI_ACCESS_LEVELS = {
    CLI_ACCESS_LEVEL_READ_ONLY,
    CLI_ACCESS_LEVEL_READ_WRITE,
}


class CLITokenAuthenticationError(Exception):
    def __init__(self, code, message, *, status=401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _token_hash(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def cli_token_scopes(access_level):
    if access_level not in CLI_ACCESS_LEVELS:
        raise ValueError(f"Unsupported CLI access level: {access_level}")

    scopes = [
        CLI_DOCUMENTS_READ_SCOPE,
        CLI_SEARCH_SCOPE,
        CLI_RAG_SCOPE,
        CLI_STATUS_SCOPE,
    ]
    if access_level == CLI_ACCESS_LEVEL_READ_WRITE:
        scopes.append(CLI_DOCUMENTS_WRITE_SCOPE)
    return scopes


def cli_token_access_level(token):
    if token.has_scope(CLI_DOCUMENTS_WRITE_SCOPE):
        return CLI_ACCESS_LEVEL_READ_WRITE
    return CLI_ACCESS_LEVEL_READ_ONLY


def issue_cli_token(*, user, name, access_level=CLI_ACCESS_LEVEL_READ_WRITE):
    raw_token = f"{CLI_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token = CLIToken.objects.create(
        user=user,
        name=name,
        key_hash=_token_hash(raw_token),
        prefix=raw_token[:16],
        scopes=cli_token_scopes(access_level),
    )
    return token, raw_token


def authenticate_cli_token(raw_token, *, required_scope=None):
    if not raw_token or not raw_token.startswith(CLI_TOKEN_PREFIX):
        raise CLITokenAuthenticationError(
            "INVALID_CLI_TOKEN",
            "Invalid or inactive CLI token.",
        )

    try:
        token = CLIToken.objects.select_related("user").get(
            key_hash=_token_hash(raw_token),
            is_active=True,
        )
    except CLIToken.DoesNotExist as exc:
        raise CLITokenAuthenticationError(
            "INVALID_CLI_TOKEN",
            "Invalid or inactive CLI token.",
        ) from exc

    if not token.user.is_active:
        raise CLITokenAuthenticationError(
            "ACCOUNT_INACTIVE",
            "User account is inactive.",
            status=403,
        )
    if not token.user.email_verified:
        raise CLITokenAuthenticationError(
            "EMAIL_VERIFICATION_REQUIRED",
            "Email verification is required.",
            status=403,
        )
    if required_scope and not token.has_scope(required_scope):
        raise CLITokenAuthenticationError(
            "CLI_TOKEN_SCOPE_REQUIRED",
            f"The CLI token requires the '{required_scope}' scope.",
            status=403,
        )

    CLIToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token


def bearer_token_from_request(request):
    parts = request.META.get("HTTP_AUTHORIZATION", "").strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1]


def resolve_cli_workspace_context(request, user):
    """Resolve an ACTIVE workspace for Bearer requests.

    CLI tokens remain account-scoped. The optional stable workspace UUID
    selects a team; omitted values preserve v1 compatibility by selecting the
    account's personal workspace.
    """
    from django.core.exceptions import ValidationError
    from workspaces.models import Workspace, WorkspaceMembership

    requested_uid = request.META.get("HTTP_X_DOTORI_WORKSPACE", "").strip()
    memberships = WorkspaceMembership.objects.filter(
        user=user,
        status=WorkspaceMembership.STATUS_ACTIVE,
    ).select_related("workspace")
    try:
        membership = (
            memberships.filter(workspace__uid=requested_uid).first()
            if requested_uid
            else memberships.filter(workspace__kind=Workspace.KIND_PERSONAL).first()
        )
    except (ValidationError, ValueError):
        membership = None
    if membership is None:
        raise CLITokenAuthenticationError(
            "WORKSPACE_ACCESS_DENIED",
            "The requested workspace is unavailable or not an active membership.",
            status=403,
        )
    request.workspace = membership.workspace
    request.workspace_membership = membership
    return membership


def cli_token_required(required_scope):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            raw_token = bearer_token_from_request(request)
            if not raw_token:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": {
                            "code": "CLI_AUTHENTICATION_REQUIRED",
                            "message": "A CLI Bearer token is required.",
                            "details": {},
                        },
                    },
                    status=401,
                )
            try:
                token = authenticate_cli_token(
                    raw_token,
                    required_scope=required_scope,
                )
            except CLITokenAuthenticationError as exc:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "details": {},
                        },
                    },
                    status=exc.status,
                )

            request.user = token.user
            request._cached_user = token.user
            request.cli_token = token
            if required_scope in {
                CLI_DOCUMENTS_READ_SCOPE,
                CLI_DOCUMENTS_WRITE_SCOPE,
                CLI_SEARCH_SCOPE,
                CLI_RAG_SCOPE,
            }:
                try:
                    resolve_cli_workspace_context(request, token.user)
                except CLITokenAuthenticationError as exc:
                    return JsonResponse(
                        {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": {}}},
                        status=exc.status,
                    )
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


class CLITokenAuthentication(BaseAuthentication):
    """DRF authentication for the dedicated CLI API routes."""

    def authenticate(self, request):
        raw_token = bearer_token_from_request(request)
        if not raw_token:
            raise exceptions.AuthenticationFailed("A CLI Bearer token is required.")

        view = (request.parser_context or {}).get("view")
        required_scope = getattr(view, "required_cli_scope", None)
        try:
            token = authenticate_cli_token(
                raw_token,
                required_scope=required_scope,
            )
        except CLITokenAuthenticationError as exc:
            if exc.status == 403:
                raise exceptions.PermissionDenied(exc.message, code=exc.code) from exc
            raise exceptions.AuthenticationFailed(exc.message, code=exc.code) from exc
        try:
            resolve_cli_workspace_context(request._request, token.user)
        except CLITokenAuthenticationError as exc:
            raise exceptions.PermissionDenied(exc.message, code=exc.code) from exc
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"


def serialize_cli_token(token):
    return {
        "uid": str(token.uid),
        "name": token.name,
        "prefix": token.prefix,
        "scopes": list(token.scopes or []),
        "access_level": cli_token_access_level(token),
        "is_active": token.is_active,
        "created_at": token.created_at.isoformat(),
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
    }
