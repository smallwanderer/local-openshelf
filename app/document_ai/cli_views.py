from asgiref.sync import sync_to_async
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from rest_framework import permissions

from accounts.cli_tokens import (
    CLI_RAG_SCOPE,
    CLI_SEARCH_SCOPE,
    CLI_STATUS_SCOPE,
    CLITokenAuthenticationError,
    CLITokenAuthentication,
    authenticate_cli_token,
    bearer_token_from_request,
    cli_token_required,
    resolve_cli_workspace_context,
)
from accounts.client_identity import serialize_client_identity
from document_ai.search.views import RAGStreamView, VectorSearchView
from document_ai.status_views import server_policy


@cli_token_required(CLI_STATUS_SCOPE)
@require_GET
def cli_status(request):
    return server_policy(request)


@cli_token_required(None)
@require_GET
def cli_identity(request):
    return JsonResponse(serialize_client_identity(request.cli_token))


class CLIVectorSearchView(VectorSearchView):
    authentication_classes = [CLITokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    required_cli_scope = CLI_SEARCH_SCOPE


@method_decorator(csrf_exempt, name="dispatch")
class CLIRAGStreamView(RAGStreamView):
    async def post(self, request, *args, **kwargs):
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
            token = await sync_to_async(
                authenticate_cli_token,
                thread_sensitive=True,
            )(raw_token, required_scope=CLI_RAG_SCOPE)
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
        request._acached_user = token.user
        request.cli_token = token
        try:
            await sync_to_async(resolve_cli_workspace_context, thread_sensitive=True)(request, token.user)
        except CLITokenAuthenticationError as exc:
            return JsonResponse(
                {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": {}}},
                status=exc.status,
            )
        return await super().post(request, *args, **kwargs)
