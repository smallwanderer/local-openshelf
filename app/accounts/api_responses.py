from django.http import JsonResponse


API_PATH_PREFIXES = (
    "/files/api/",
    "/api/accounts/",
    "/api/workspaces/v1/",
    "/api/document-ai/v1/",
    "/api/sync/v1/",
    "/api/cli/v1/",
)


def is_json_api_request(request) -> bool:
    return any(request.path.startswith(prefix) for prefix in API_PATH_PREFIXES)


def api_error_response(
    code: str,
    message: str,
    *,
    status: int,
    details=None,
) -> JsonResponse:
    return JsonResponse(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "details": details if details is not None else {},
            },
        },
        status=status,
    )


def session_access_error(user, *, require_verified: bool = True):
    if not getattr(user, "is_authenticated", False):
        return api_error_response(
            "AUTHENTICATION_REQUIRED",
            "Authentication is required.",
            status=401,
        )
    if require_verified and not getattr(user, "email_verified", False):
        return api_error_response(
            "EMAIL_VERIFICATION_REQUIRED",
            "Email verification is required.",
            status=403,
        )
    return None


def drf_exception_handler(exc, context):
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None

    details = response.data
    detail_message = ""
    if isinstance(details, dict) and "detail" in details:
        detail_message = str(details["detail"])

    if response.status_code == 400:
        code = "INVALID_REQUEST"
        message = detail_message or "Request validation failed."
    elif response.status_code == 401:
        code = "AUTHENTICATION_REQUIRED"
        message = detail_message or "Authentication is required."
    elif response.status_code == 403 and detail_message.lower().startswith("csrf failed"):
        code = "CSRF_FAILED"
        message = "CSRF validation failed. Refresh the session and try again."
    elif response.status_code == 403:
        code = "PERMISSION_DENIED"
        message = detail_message or "Permission denied."
    elif response.status_code == 404:
        code = "NOT_FOUND"
        message = detail_message or "The requested resource was not found."
    elif response.status_code == 405:
        code = "METHOD_NOT_ALLOWED"
        message = detail_message or "The HTTP method is not allowed."
    else:
        code = "API_ERROR"
        message = detail_message or "The API request failed."

    response.data = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
    }
    return response
