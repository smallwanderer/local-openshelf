import json
import uuid

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.api_responses import api_error_response
from accounts.cli_tokens import (
    CLI_ACCESS_LEVEL_READ_ONLY,
    CLI_ACCESS_LEVEL_READ_WRITE,
    CLI_ACCESS_LEVELS,
    issue_cli_token,
    serialize_cli_token,
)
from accounts.decorators import api_login_required, email_verification_required
from accounts.models import APIToken, CLIToken


def _session_payload(request):
    user = request.user
    authenticated = bool(user.is_authenticated)
    return {
        "ok": True,
        "auth": {
            "mode": "required" if settings.LOGIN_REQUIRED else "local",
            "login_required": bool(settings.LOGIN_REQUIRED),
            "authenticated": authenticated,
        },
        "user": (
            {
                "email": user.email,
                "display_name": user.display_name,
                "email_verified": bool(user.email_verified),
                "is_staff": bool(user.is_staff),
            }
            if authenticated
            else None
        ),
    }


def _json_object(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@require_GET
@ensure_csrf_cookie
def session_bootstrap(request):
    """Return the current session state and ensure the SPA has a CSRF cookie."""

    return JsonResponse(_session_payload(request))


@require_POST
@ensure_csrf_cookie
def session_login(request):
    if not settings.LOGIN_REQUIRED:
        return api_error_response(
            "LOGIN_NOT_REQUIRED",
            "Password login is disabled in local mode.",
            status=409,
        )

    payload = _json_object(request)
    if payload is None:
        return api_error_response(
            "INVALID_REQUEST",
            "Expected a JSON object.",
            status=400,
        )

    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return api_error_response(
            "INVALID_REQUEST",
            "Email and password are required.",
            status=400,
            details={
                "email": [] if email else ["This field is required."],
                "password": [] if password else ["This field is required."],
            },
        )

    user = authenticate(request, username=email, password=password)
    if user is None:
        return api_error_response(
            "INVALID_CREDENTIALS",
            "The email or password is incorrect.",
            status=401,
        )
    if not user.email_verified:
        return api_error_response(
            "EMAIL_VERIFICATION_REQUIRED",
            "Email verification is required.",
            status=403,
        )

    login(request, user)
    return JsonResponse(_session_payload(request))


@require_POST
def session_logout(request):
    if not settings.LOGIN_REQUIRED:
        return api_error_response(
            "LOGOUT_NOT_AVAILABLE",
            "The local profile remains active while login is disabled.",
            status=409,
        )

    logout(request)
    return JsonResponse(_session_payload(request))


@api_login_required
@email_verification_required
@require_http_methods(["GET", "POST"])
def cli_tokens(request):
    if request.method == "GET":
        tokens = request.user.cli_tokens.all().order_by("-created_at")
        return JsonResponse({
            "ok": True,
            "tokens": [serialize_cli_token(token) for token in tokens],
        })

    payload = _json_object(request)
    if payload is None:
        return api_error_response(
            "INVALID_REQUEST",
            "Expected a JSON object.",
            status=400,
        )

    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 128:
        return api_error_response(
            "INVALID_REQUEST",
            "Token name is required and must not exceed 128 characters.",
            status=400,
            details={"name": ["Enter a name between 1 and 128 characters."]},
        )
    enable_sync = payload.get("enable_sync", False)
    if not isinstance(enable_sync, bool):
        return api_error_response(
            "INVALID_REQUEST",
            "enable_sync must be a boolean.",
            status=400,
            details={"enable_sync": ["Enter true or false."]},
        )
    if enable_sync:
        return api_error_response(
            "INVALID_REQUEST",
            "Sync credentials must be issued as a separate sync token.",
            status=400,
            details={"enable_sync": ["Issue a folder sync token instead."]},
        )
    access_level = str(
        payload.get("access_level") or CLI_ACCESS_LEVEL_READ_WRITE
    ).strip()
    if access_level not in CLI_ACCESS_LEVELS:
        return api_error_response(
            "INVALID_REQUEST",
            "access_level must be 'read_only' or 'read_write'.",
            status=400,
            details={"access_level": ["Choose read_only or read_write."]},
        )

    token, secret = issue_cli_token(
        user=request.user,
        name=name,
        access_level=access_level,
    )
    response = JsonResponse(
        {
            "ok": True,
            "token": serialize_cli_token(token),
            "secret": secret,
            "secret_display": "once",
        },
        status=201,
    )
    response["Cache-Control"] = "no-store"
    return response


@api_login_required
@email_verification_required
@require_http_methods(["DELETE"])
def revoke_cli_token(request, uid):
    updated = CLIToken.objects.filter(
        uid=uid,
        user=request.user,
        is_active=True,
    ).update(is_active=False)
    if not updated:
        return api_error_response(
            "CLI_TOKEN_NOT_FOUND",
            "The CLI token was not found or is already inactive.",
            status=404,
        )
    return JsonResponse({"ok": True, "uid": str(uid), "is_active": False})


def _serialize_access_token(token):
    if isinstance(token, CLIToken):
        payload = serialize_cli_token(token)
        return {
            "id": payload["uid"],
            "token_type": "cli",
            "name": payload["name"],
            "prefix": payload["prefix"],
            "scopes": payload["scopes"],
            "access_level": payload["access_level"],
            "is_active": payload["is_active"],
            "created_at": payload["created_at"],
            "last_used_at": payload["last_used_at"],
        }
    return {
        "id": str(token.pk),
        "token_type": "sync",
        "name": token.name,
        "prefix": token.key[:12],
        "scopes": ["sync"],
        "access_level": "sync",
        "is_active": token.is_active,
        "created_at": token.created_at.isoformat(),
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
    }


@api_login_required
@email_verification_required
@require_http_methods(["GET", "POST"])
def access_tokens(request):
    """List and issue user credentials by their external client purpose."""

    if request.method == "GET":
        tokens = [
            *(request.user.cli_tokens.all()),
            *(request.user.api_tokens.all()),
        ]
        tokens.sort(key=lambda token: token.created_at, reverse=True)
        return JsonResponse({
            "ok": True,
            "tokens": [_serialize_access_token(token) for token in tokens],
        })

    payload = _json_object(request)
    if payload is None:
        return api_error_response(
            "INVALID_REQUEST",
            "Expected a JSON object.",
            status=400,
        )

    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 128:
        return api_error_response(
            "INVALID_REQUEST",
            "Token name is required and must not exceed 128 characters.",
            status=400,
            details={"name": ["Enter a name between 1 and 128 characters."]},
        )

    token_type = str(payload.get("token_type") or "").strip()
    if token_type == "cli":
        access_level = str(
            payload.get("access_level") or CLI_ACCESS_LEVEL_READ_ONLY
        ).strip()
        if access_level not in CLI_ACCESS_LEVELS:
            return api_error_response(
                "INVALID_REQUEST",
                "access_level must be 'read_only' or 'read_write'.",
                status=400,
                details={"access_level": ["Choose read_only or read_write."]},
            )
        token, secret = issue_cli_token(
            user=request.user,
            name=name,
            access_level=access_level,
        )
    elif token_type == "sync":
        requested_access_level = payload.get("access_level")
        if requested_access_level not in (None, "", "sync"):
            return api_error_response(
                "INVALID_REQUEST",
                "A sync token cannot include CLI access levels.",
                status=400,
                details={"access_level": ["Remove the CLI access level."]},
            )
        token = APIToken.objects.create(user=request.user, name=name)
        secret = token.key
    else:
        return api_error_response(
            "INVALID_REQUEST",
            "token_type must be 'cli' or 'sync'.",
            status=400,
            details={"token_type": ["Choose cli or sync."]},
        )

    response = JsonResponse(
        {
            "ok": True,
            "token": _serialize_access_token(token),
            "secret": secret,
            "secret_display": "once",
        },
        status=201,
    )
    response["Cache-Control"] = "no-store"
    return response


@api_login_required
@email_verification_required
@require_http_methods(["DELETE"])
def revoke_access_token(request, token_type, identifier):
    if token_type == "cli":
        try:
            token_uid = uuid.UUID(identifier)
        except (TypeError, ValueError):
            updated = 0
        else:
            updated = CLIToken.objects.filter(
                uid=token_uid,
                user=request.user,
                is_active=True,
            ).update(is_active=False)
    elif token_type == "sync":
        if identifier.isdigit():
            deleted, _ = APIToken.objects.filter(
                pk=int(identifier),
                user=request.user,
            ).delete()
            updated = deleted
        else:
            updated = 0
    else:
        return api_error_response(
            "INVALID_REQUEST",
            "token_type must be 'cli' or 'sync'.",
            status=400,
        )

    if not updated:
        return api_error_response(
            "TOKEN_NOT_FOUND",
            "The token was not found or is already inactive.",
            status=404,
        )
    return JsonResponse({
        "ok": True,
        "id": identifier,
        "token_type": token_type,
        "is_active": False,
    })


@api_login_required
@email_verification_required
@require_http_methods(["DELETE"])
def delete_access_token(request, token_type, identifier):
    """Permanently remove an already-revoked token. Active tokens must be
    revoked first so deactivation and deletion stay separate, deliberate steps."""

    if token_type == "cli":
        try:
            token_uid = uuid.UUID(identifier)
        except (TypeError, ValueError):
            deleted = 0
        else:
            deleted, _ = CLIToken.objects.filter(
                uid=token_uid,
                user=request.user,
                is_active=False,
            ).delete()
    elif token_type == "sync":
        if identifier.isdigit():
            deleted, _ = APIToken.objects.filter(
                pk=int(identifier),
                user=request.user,
                is_active=False,
            ).delete()
        else:
            deleted = 0
    else:
        return api_error_response(
            "INVALID_REQUEST",
            "token_type must be 'cli' or 'sync'.",
            status=400,
        )

    if not deleted:
        return api_error_response(
            "TOKEN_NOT_FOUND",
            "The token was not found or must be revoked before it can be deleted.",
            status=404,
        )
    return JsonResponse({
        "ok": True,
        "id": identifier,
        "token_type": token_type,
        "deleted": True,
    })
