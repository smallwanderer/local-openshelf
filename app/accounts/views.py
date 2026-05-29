from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.conf import settings

from .forms import EmailAuthenticationForm, ResendVerificationEmailForm, UserRegistrationForm
from .models import User, APIToken, SyncQuota
from .services import send_account_activation_email
from .tokens import account_activation_token


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
        if action == "create_token":
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
        elif action == "change_password":
            current_pw = request.POST.get("current_password", "")
            new_pw = request.POST.get("new_password", "")
            new_pw2 = request.POST.get("new_password2", "")
            if not request.user.check_password(current_pw):
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
        elif action == "delete_account":
            confirm_pw = request.POST.get("confirm_password", "")
            if not request.user.check_password(confirm_pw):
                messages.error(request, "비밀번호가 올바르지 않습니다.")
                return redirect("accounts:settings")
            from django.contrib.auth import logout
            user = request.user
            logout(request)
            user.delete()
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
    }
    return render(request, "accounts/settings.html", ctx)


