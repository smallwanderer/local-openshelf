from django.views.decorators.csrf import csrf_exempt

from accounts.cli_tokens import (
    CLI_DOCUMENTS_READ_SCOPE,
    CLI_DOCUMENTS_WRITE_SCOPE,
    cli_token_required,
)
from files.api_v1.file_views import file_list, upload_file


@csrf_exempt
@cli_token_required(CLI_DOCUMENTS_READ_SCOPE)
def cli_document_list(request):
    return file_list(request)


@csrf_exempt
@cli_token_required(CLI_DOCUMENTS_WRITE_SCOPE)
def cli_upload(request):
    return upload_file(request)
