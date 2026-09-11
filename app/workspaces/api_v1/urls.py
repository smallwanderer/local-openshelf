from django.urls import path

from . import views

app_name = "workspaces_api"

urlpatterns = [
    path("", views.workspace_collection, name="collection"),
    path("switch/", views.switch_workspace, name="switch"),
    path("current/", views.current_workspace, name="current"),
    path("current/members/", views.member_list, name="member-list"),
    path("current/members/<int:user_id>/", views.member_detail, name="member-detail"),
    path("current/invite-code/", views.issue_invite_code, name="invite-code-issue"),
    path("invite-code/redeem/", views.redeem_invite_code, name="invite-code-redeem"),
    path("current/invites/", views.create_invite, name="invite-create"),
    path("current/quality-profile/", views.quality_profile, name="quality-profile"),
    path("current/quality-profile/draft/", views.quality_profile_draft, name="quality-profile-draft"),
    path("current/quality-profile/draft/discard/", views.quality_profile_draft_discard, name="quality-profile-draft-discard"),
    path("current/quality-profile/draft/preview-prompt/", views.quality_profile_draft_preview_prompt, name="quality-profile-draft-preview-prompt"),
    path("current/quality-profile/apply/", views.quality_profile_apply, name="quality-profile-apply"),
    path("current/quality-profile/evaluate/", views.quality_profile_evaluate, name="quality-profile-evaluate"),
    path("current/quality-profile/versions/", views.quality_profile_versions, name="quality-profile-versions"),
    path("current/llm-settings/", views.llm_settings, name="llm-settings"),
    path("current/llm-settings/select/", views.llm_settings_select, name="llm-settings-select"),
    path("current/evaluation-datasets/", views.evaluation_datasets, name="evaluation-datasets"),
    path("current/evaluation-runs/<uuid:run_uid>/", views.evaluation_run_detail, name="evaluation-run-detail"),
    path("invites/inbox/", views.invite_inbox, name="invite-inbox"),
    path("invites/<int:invitation_id>/accept/", views.accept_invite, name="invite-accept"),
    path("invites/<int:invitation_id>/decline/", views.decline_invite, name="invite-decline"),
]
