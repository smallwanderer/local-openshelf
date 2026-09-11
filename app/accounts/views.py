from pathlib import Path

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import get_template
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.conf import settings

from .forms import EmailAuthenticationForm, ResendVerificationEmailForm, UserRegistrationForm
from .models import User, APIToken, SyncQuota
from .api_responses import api_error_response, is_json_api_request
from .services import create_local_profile, send_account_activation_email, update_account_email
from .tokens import account_activation_token
from document_ai.models import LLMEndpoint
from document_ai.services.llm_endpoint_service import (
    check_llm_endpoint,
    delete_llm_endpoint,
    get_user_llm_settings_context,
    set_user_rag_model,
    upsert_llm_endpoint,
)
from workspaces.models import WorkspaceMembership


TERMS_VERSION = "2026-06-03"
PRIVACY_VERSION = "2026-06-03"

LEGAL_DOCUMENTS = {
    "terms": {
        "title": "Terms of Use",
        "filename": "terms.md",
        "version": TERMS_VERSION,
    },
    "privacy": {
        "title": "Privacy Policy",
        "filename": "privacy.md",
        "version": PRIVACY_VERSION,
    },
}

ERROR_DETAILS = {
    400: {
        "title": "잘못된 요청",
        "message": "요청 내용을 처리할 수 없습니다. 입력 내용이나 요청 주소를 확인해 주세요.",
    },
    403: {
        "title": "접근 권한 없음",
        "message": "이 페이지 또는 작업에 접근할 권한이 없습니다.",
    },
    404: {
        "title": "페이지를 찾을 수 없음",
        "message": "요청한 페이지가 없거나 이동 또는 삭제되었습니다.",
    },
    500: {
        "title": "서버 오류",
        "message": "요청 처리 중 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.",
    },
}


def _render_error(request, status_code, *, message=None, code=None, details=None):
    page_details = ERROR_DETAILS[status_code]
    if is_json_api_request(request):
        error_codes = {
            400: "INVALID_REQUEST",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            500: "INTERNAL_ERROR",
        }
        return api_error_response(
            code or error_codes[status_code],
            message or page_details["message"],
            status=status_code,
            details=details if details is not None else {},
        )
    content = get_template("errors/error.html").render(
        {
            "status_code": status_code,
            "error_title": page_details["title"],
            "error_message": message or page_details["message"],
        }
    )
    return HttpResponse(content, status=status_code)


def bad_request(request, exception=None):
    return _render_error(request, 400)


def permission_denied(request, exception=None):
    return _render_error(request, 403)


def page_not_found(request, exception=None):
    return _render_error(request, 404)


def server_error(request):
    return _render_error(request, 500)


def csrf_failure(request, reason=""):
    return _render_error(
        request,
        403,
        code="CSRF_FAILED",
        message="보안 확인에 실패했습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.",
        details={"reason": reason} if settings.DEBUG and reason else {},
    )


def legal_document_view(request, document):
    spec = LEGAL_DOCUMENTS.get(document)
    if spec is None:
        raise Http404("Legal document not found.")

    document_path = Path(__file__).resolve().parent / "legal" / spec["filename"]
    try:
        content = document_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Http404("Legal document not found.")

    return render(
        request,
        "accounts/legal_document.html",
        {
            "title": spec["title"],
            "content": content,
            "version": spec["version"],
        },
    )


def signup_view(request):
    if request.method == "POST":
        form = UserRegistrationForm(request.POST)

        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data["password"])
            
            if getattr(settings, "REQUIRE_EMAIL_VERIFICATION", True):
                user.is_active = False
                user.email_verified = False
                user.save()

                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = account_activation_token.make_token(user)
                send_account_activation_email(request, user, uid, token)

                return render(request, "accounts/signup_done.html")
            else:
                user.is_active = True
                user.email_verified = True
                user.save()
                messages.success(request, "Account created successfully. You can now log in.")
                return redirect("accounts:login")

    else:
        form = UserRegistrationForm()

    return render(request, "accounts/signup.html", {"form": form})


def verify_email(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user and account_activation_token.check_token(user, token):
        user.email_verified = True
        user.is_active = True
        user.save()
        return render(request, "accounts/verify_success.html")

    return render(request, "accounts/verify_fail.html")


def resend_verification_email(request):
    if request.method == "POST":
        form = ResendVerificationEmailForm(request.POST)

        if form.is_valid():
            email = form.cleaned_data["email"]
            try:
                user = User.objects.get(email=email)
                if not user:
                    messages.error(request, "We could not find an account for that email address.")
                    return redirect("accounts:signup")

                if user.email_verified:
                    messages.info(request, "This account has already been verified.")
                    return redirect("accounts:login")

                uid = urlsafe_base64_encode(force_bytes(user.pk))
                token = account_activation_token.make_token(user)
                send_account_activation_email(request, user, uid, token)

                messages.success(request, "A new verification email has been sent.")
                return render(request, "accounts/signup_done.html")

            except User.DoesNotExist:
                messages.error(request, "We could not find an account for that email address.")
                return redirect("accounts:signup")

    else:
        form = ResendVerificationEmailForm()

    return render(request, "accounts/resend_verification.html", {"form": form})


def verification_required_view(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    if request.user.email_verified:
        return redirect("files:index")

    return render(request, "accounts/verification_required.html")


def _switch_to(request, user):
    user.backend = "django.contrib.auth.backends.ModelBackend"
    login(request, user)
    request.session["active_profile_id"] = user.id


def switch_account_view(request):
    """Password-less profile picker, only reachable while LOGIN_REQUIRED is off."""
    if settings.LOGIN_REQUIRED:
        raise Http404()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "switch":
            try:
                target = User.objects.get(pk=request.POST.get("user_id"), is_active=True)
            except (User.DoesNotExist, ValueError, TypeError):
                messages.error(request, "선택한 계정을 찾을 수 없습니다.")
                return redirect("accounts:switch")
            _switch_to(request, target)
            return redirect("files:index")
        elif action == "create":
            try:
                new_user = create_local_profile(request.POST.get("display_name", ""))
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect("accounts:switch")
            _switch_to(request, new_user)
            return redirect("files:index")

    profiles = User.objects.filter(is_active=True).order_by("email")
    return render(request, "accounts/switch.html", {"profiles": profiles})


class SigninView(LoginView):
    template_name = "accounts/signin.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


class SignoutView(LogoutView):
    template_name = "accounts/signout.html"


@login_required
def settings_view(request):
    new_token_key = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "update_display_name":
            display_name = request.POST.get("display_name", "").strip()[:50]
            request.user.display_name = display_name
            request.user.save(update_fields=["display_name"])
            messages.success(request, "표시 이름이 변경되었습니다.")
            return redirect("accounts:settings")
        elif action == "create_token":
            name = request.POST.get("name", "").strip()
            if name:
                token_obj = APIToken.objects.create(user=request.user, name=name)
                new_token_key = token_obj.key
                messages.success(request, "API 토큰이 성공적으로 발급되었습니다.")
            else:
                messages.error(request, "토큰 이름을 입력해주세요.")
        elif action == "delete_token":
            token_id = request.POST.get("token_id")
            if token_id:
                APIToken.objects.filter(id=token_id, user=request.user).delete()
                messages.success(request, "API 토큰이 삭제되었습니다.")
            return redirect("accounts:settings")
        elif action == "toggle_token":
            token_id = request.POST.get("token_id")
            if token_id:
                try:
                    t = APIToken.objects.get(id=token_id, user=request.user)
                    t.is_active = not t.is_active
                    t.save(update_fields=["is_active"])
                except APIToken.DoesNotExist:
                    pass
            return redirect("accounts:settings")
        elif action == "change_email":
            try:
                update_account_email(request.user, request.POST.get("email", ""))
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
            else:
                messages.success(request, "이메일이 변경되었습니다.")
            return redirect("accounts:settings")
        elif action == "change_password":
            current_pw = request.POST.get("current_password", "")
            new_pw = request.POST.get("new_password", "")
            new_pw2 = request.POST.get("new_password2", "")
            # A password-less profile (e.g. created via the no-login account
            # switcher) has nothing to verify, so skip straight to setting one.
            if request.user.has_usable_password() and not request.user.check_password(current_pw):
                messages.error(request, "현재 비밀번호가 올바르지 않습니다.")
            elif len(new_pw) < 8:
                messages.error(request, "새 비밀번호는 8자 이상이어야 합니다.")
            elif new_pw != new_pw2:
                messages.error(request, "새 비밀번호가 일치하지 않습니다.")
            else:
                request.user.set_password(new_pw)
                request.user.save()
                from django.contrib.auth import update_session_auth_hash
                update_session_auth_hash(request, request.user)
                messages.success(request, "비밀번호가 변경되었습니다.")
            return redirect("accounts:settings")
        elif action in {"create_llm_endpoint", "delete_llm_endpoint", "check_llm_endpoint", "set_rag_model"}:
            is_admin = (
                request.workspace_membership is not None
                and request.workspace_membership.role == WorkspaceMembership.ROLE_ADMIN
            )
            if not is_admin:
                messages.error(request, "이 설정은 워크스페이스 관리자만 변경할 수 있습니다.")
                return redirect("accounts:settings")

        if action == "create_llm_endpoint":
            endpoint, created = upsert_llm_endpoint(
                workspace=request.workspace,
                owner=request.user,
                name=request.POST.get("endpoint_name", ""),
                endpoint_type=request.POST.get("endpoint_type", LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE),
                base_url=request.POST.get("base_url", ""),
                default_model=request.POST.get("default_model", ""),
                api_key=request.POST.get("api_key", ""),
            )
            if endpoint is None:
                messages.error(request, "Endpoint 이름, URL, 기본 모델을 입력해주세요.")
            else:
                checked = check_llm_endpoint(workspace=request.workspace, endpoint_id=str(endpoint.id))
                action_label = "등록" if created else "갱신"
                if checked and checked.last_check_status == "ok":
                    messages.success(request, f"AI endpoint가 {action_label}되었습니다. {checked.last_check_message}")
                elif checked:
                    messages.warning(request, f"AI endpoint가 {action_label}되었지만 연결 확인에 실패했습니다. {checked.last_check_message}")
                else:
                    messages.success(request, f"AI endpoint가 {action_label}되었습니다.")
            return redirect("accounts:settings")
        elif action == "delete_llm_endpoint":
            if delete_llm_endpoint(workspace=request.workspace, endpoint_id=request.POST.get("endpoint_id")):
                messages.success(request, "AI endpoint가 삭제되었습니다.")
            return redirect("accounts:settings")
        elif action == "check_llm_endpoint":
            endpoint = check_llm_endpoint(workspace=request.workspace, endpoint_id=request.POST.get("endpoint_id"))
            if endpoint is None:
                messages.error(request, "확인할 AI endpoint를 찾을 수 없습니다.")
            elif endpoint.last_check_status == "ok":
                messages.success(request, f"{endpoint.name}: {endpoint.last_check_message}")
            else:
                messages.error(request, f"{endpoint.name}: {endpoint.last_check_message}")
            return redirect("accounts:settings")
        elif action == "set_rag_model":
            set_user_rag_model(
                workspace=request.workspace,
                owner=request.user,
                endpoint_id=request.POST.get("rag_endpoint_id"),
                rag_model=request.POST.get("rag_model", ""),
            )
            messages.success(request, "RAG 답변 모델 설정이 저장되었습니다.")
            return redirect("accounts:settings")
        elif action == "delete_account":
            confirm_pw = request.POST.get("confirm_password", "")
            if not request.user.check_password(confirm_pw):
                messages.error(request, "비밀번호가 올바르지 않습니다.")
                return redirect("accounts:settings")
            from django.core.exceptions import ValidationError
            from django.contrib.auth import logout
            from workspaces.services import delete_account_with_personal_data
            user = request.user
            try:
                delete_account_with_personal_data(user=user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return redirect("accounts:settings")
            logout(request)
            messages.success(request, "계정이 삭제되었습니다.")
            return redirect("accounts:login")

    tokens = request.user.api_tokens.all().order_by("-created_at")

    # Sync quota
    quota, _ = SyncQuota.objects.get_or_create(user=request.user)
    used_mb = round(quota.used_size / 1024 / 1024, 1)
    total_gb = round(quota.total_size / 1024 / 1024 / 1024, 1)
    quota_pct = round(quota.used_size / quota.total_size * 100, 1) if quota.total_size else 0

    # File count
    file_count = request.user.files.filter(is_trashed=False).count() if hasattr(request.user, "files") else 0

    ctx = {
        "tokens": tokens,
        "new_token_key": new_token_key,
        "quota_used_mb": used_mb,
        "quota_total_gb": total_gb,
        "quota_pct": quota_pct,
        "file_count": file_count,
        "workspace": request.workspace,
        "is_workspace_admin": (
            request.workspace_membership is not None
            and request.workspace_membership.role == WorkspaceMembership.ROLE_ADMIN
        ),
        **get_user_llm_settings_context(request.workspace, owner=request.user),
    }
    return render(request, "accounts/settings.html", ctx)
