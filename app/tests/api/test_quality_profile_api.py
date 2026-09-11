import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from document_ai.rag.generation import _build_generation_prompts
from document_ai.rag.prompt_profiles import get_effective_prompt_policy
from document_ai.search.profiles import get_effective_generation_config
from workspaces.models import WorkspaceMembership, WorkspaceQualityProfileRevision
from workspaces.services import create_team_workspace


User = get_user_model()

pytestmark = pytest.mark.unit

BASE = "/api/workspaces/v1/current/quality-profile"


class QualityProfileApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="quality-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="quality-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="Quality Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def request_json(self, method, path, payload=None):
        return getattr(self.client, method)(
            path,
            data=json.dumps(payload or {}),
            content_type="application/json",
        )

    def login_in_workspace(self, user):
        self.client.force_login(user)
        response = self.request_json(
            "post",
            "/api/workspaces/v1/switch/",
            {"workspace_uid": str(self.workspace.uid)},
        )
        self.assertEqual(response.status_code, 200)

    def save_draft(self, *, retrieval=None, generation=None, prompt_policy=None, expected_revision=0, note="candidate"):
        payload = {"expected_revision": expected_revision, "note": note}
        if retrieval is not None:
            payload["retrieval"] = retrieval
        if generation is not None:
            payload["generation"] = generation
        if prompt_policy is not None:
            payload["prompt_policy"] = prompt_policy
        return self.request_json("patch", f"{BASE}/draft/", payload)

    def apply(self, *, expected_revision, allow_unverified=False, evaluation_run_uid=None, note="apply"):
        return self.request_json(
            "post",
            f"{BASE}/apply/",
            {
                "expected_revision": expected_revision,
                "allow_unverified": allow_unverified,
                "evaluation_run_uid": evaluation_run_uid,
                "note": note,
            },
        )

    # -- basic envelope -----------------------------------------------------

    def test_get_creates_workspace_scoped_active_profile(self):
        self.login_in_workspace(self.admin)

        response = self.client.get(f"{BASE}/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["workspace_uid"], str(self.workspace.uid))
        self.assertEqual(body["active"]["version"], 1)
        self.assertIsNone(body["draft"])
        self.assertIn("candidate_multiplier", body["schema"]["retrieval"])
        self.assertIn("temperature", body["schema"]["generation"])
        self.assertEqual(
            WorkspaceQualityProfileRevision.objects.filter(
                workspace=self.workspace,
                status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            ).count(),
            1,
        )

    def test_member_can_read_but_cannot_change_profile(self):
        self.login_in_workspace(self.member)

        response = self.client.get(f"{BASE}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["permissions"]["can_edit"])

        response = self.save_draft(retrieval={"overrides": {"search_top_k": 9}, "reset_fields": []})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    # -- draft lifecycle ------------------------------------------------------

    def test_draft_uses_optimistic_revision_and_can_be_discarded(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        created = self.save_draft(retrieval={"overrides": {"search_top_k": 9}, "reset_fields": []})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["draft"]["revision"], 1)
        self.assertEqual(created.json()["draft"]["retrieval"]["effective"]["search_top_k"], 9)
        self.assertEqual(created.json()["draft"]["changed_axes"], ["retrieval"])

        stale = self.save_draft(retrieval={"overrides": {"search_top_k": 10}, "reset_fields": []})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "PROFILE_REVISION_CONFLICT")

        discarded = self.request_json("post", f"{BASE}/draft/discard/", {"expected_revision": 1})
        self.assertEqual(discarded.status_code, 200)
        self.assertIsNone(discarded.json()["draft"])

    def test_saving_with_no_axes_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        response = self.request_json("patch", f"{BASE}/draft/", {"expected_revision": 0, "note": "empty"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_saving_identical_values_raises_no_changes(self):
        self.login_in_workspace(self.admin)
        active = self.client.get(f"{BASE}/").json()["active"]

        response = self.save_draft(retrieval={
            "overrides": {"search_top_k": active["retrieval"]["effective"]["search_top_k"]},
            "reset_fields": [],
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "PROFILE_HAS_NO_CHANGES")

    # -- retrieval + generation combined --------------------------------------

    def test_retrieval_and_generation_can_be_saved_in_one_draft(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        response = self.save_draft(
            retrieval={"overrides": {"search_top_k": 9}, "reset_fields": []},
            generation={"overrides": {"temperature": 0.7}, "reset_fields": []},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertCountEqual(body["draft"]["changed_axes"], ["retrieval", "generation"])
        self.assertEqual(body["draft"]["retrieval"]["effective"]["search_top_k"], 9)
        self.assertEqual(body["draft"]["generation"]["effective"]["temperature"], 0.7)

    def test_unverified_apply_requires_explicit_flag_and_note(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(
            self.save_draft(retrieval={"overrides": {"search_top_k": 9}, "reset_fields": []}).status_code, 200,
        )

        blocked = self.apply(expected_revision=1, allow_unverified=False, note="candidate")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "EVALUATION_REQUIRED")

        applied = self.apply(expected_revision=1, allow_unverified=True, note="manual verification")
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["active"]["version"], 2)
        self.assertEqual(body["active"]["retrieval"]["effective"]["search_top_k"], 9)
        self.assertEqual(body["active"]["validation"]["state"], "unverified")
        self.assertIsNone(body["draft"])

    def test_apply_with_mixed_axes_always_requires_allow_unverified(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(
            self.save_draft(
                retrieval={"overrides": {"search_top_k": 9}, "reset_fields": []},
                generation={"overrides": {"temperature": 0.7}, "reset_fields": []},
            ).status_code, 200,
        )

        blocked = self.apply(expected_revision=1, allow_unverified=False, note="candidate")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "EVALUATION_REQUIRED")

        applied = self.apply(expected_revision=1, allow_unverified=True, note="manual verification")
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["active"]["validation"]["state"], "unverified")

    def test_evaluation_run_uid_rejected_when_retrieval_untouched(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(
            self.save_draft(generation={"overrides": {"temperature": 0.7}, "reset_fields": []}).status_code, 200,
        )

        response = self.apply(expected_revision=1, allow_unverified=True, evaluation_run_uid="00000000-0000-0000-0000-000000000000", note="x")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "EVALUATION_REQUIRED")

    @patch("document_ai.search.views.profile_threshold_to_retriever", return_value=0.123)
    @patch("document_ai.search.views.search_documents_sync", return_value=([], {"result_count": 0}))
    def test_active_profile_is_used_by_normal_search(self, search_documents, _threshold):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(
            self.save_draft(retrieval={
                "overrides": {"dense_weight": 0.4, "sparse_weight": 0.6, "search_top_k": 9},
                "reset_fields": [],
            }).status_code, 200,
        )
        self.apply(expected_revision=1, allow_unverified=True, note="manual verification")

        response = self.request_json("post", "/api/document-ai/v1/search/", {"query": "workspace profile"})

        self.assertEqual(response.status_code, 200)
        kwargs = search_documents.call_args.kwargs
        self.assertEqual(kwargs["top_k"], 9)
        self.assertEqual(kwargs["threshold"], 0.123)
        self.assertEqual(kwargs["tuning_params"]["dense_weight"], 0.4)
        self.assertEqual(kwargs["tuning_params"]["sparse_weight"], 0.6)

    def test_invalid_weight_pair_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        response = self.save_draft(retrieval={
            "overrides": {"dense_weight": 0.8, "sparse_weight": 0.8}, "reset_fields": [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")

    def test_out_of_range_temperature_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        response = self.save_draft(generation={
            "overrides": {"max_output_tokens": 512, "temperature": 3.5, "top_p": 0.9}, "reset_fields": [],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")

    def test_applied_generation_profile_is_read_by_get_effective_generation_config(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(
            self.save_draft(generation={
                "overrides": {"max_output_tokens": 640, "temperature": 1.1, "top_p": 0.5}, "reset_fields": [],
            }).status_code, 200,
        )
        self.apply(expected_revision=1, allow_unverified=True, note="manual verification")

        effective = get_effective_generation_config(self.workspace)

        self.assertEqual(effective["max_output_tokens"], 640)
        self.assertEqual(effective["temperature"], 1.1)
        self.assertEqual(effective["top_p"], 0.5)

    # -- prompt policy --------------------------------------------------------

    def save_prompt_draft(self, *, instruction="Use a concise table for comparisons.", expected_revision=0):
        return self.save_draft(
            prompt_policy={"document_rag": {"mode": "replace", "instruction": instruction}},
            expected_revision=expected_revision,
            note="workspace answer policy",
        )

    def test_prompt_draft_can_be_previewed_and_applied(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        created = self.save_prompt_draft(instruction="Answer with a concise decision table.")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(
            created.json()["draft"]["prompt_policy"]["effective"]["document_rag"]["character_count"], 37,
        )

        preview = self.request_json(
            "post", f"{BASE}/draft/preview-prompt/", {"expected_revision": 1, "route": "document_rag"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Answer with a concise decision table.", preview.json()["assembled_prompt"])
        self.assertIn("always take priority", preview.json()["assembled_prompt"])

        applied = self.apply(expected_revision=1, allow_unverified=True, note="manually reviewed prompt")
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(
            get_effective_prompt_policy(self.workspace)["document_rag"]["instruction"],
            "Answer with a concise decision table.",
        )

    def test_applied_policy_is_used_by_generation_and_markup_is_escaped(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")
        self.assertEqual(self.save_prompt_draft(instruction="Use <brief> answers.").status_code, 200)
        self.apply(expected_revision=1, allow_unverified=True, note="reviewed")

        job = SimpleNamespace(workspace=self.workspace, language="ko", question="정책은 무엇인가요?")
        system_prompt, _, _, _ = _build_generation_prompts(job, skip_retrieval=False, context_text="[1] policy")

        self.assertIn("Use &lt;brief&gt; answers.", system_prompt)
        self.assertIn("Only answer what is supported by the evidence", system_prompt)

    def test_prompt_apply_preserves_other_profile_axes(self):
        WorkspaceQualityProfileRevision.objects.create(
            workspace=self.workspace,
            version=1,
            revision=1,
            status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            changed_axes=[WorkspaceQualityProfileRevision.AXIS_RETRIEVAL],
            retrieval_config={"search_top_k": 9},
            generation_config={"temperature": 0.4},
        )
        self.login_in_workspace(self.admin)
        self.assertEqual(self.save_prompt_draft().status_code, 200)
        self.apply(expected_revision=1, allow_unverified=True, note="reviewed")

        active = WorkspaceQualityProfileRevision.objects.get(
            workspace=self.workspace, status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
        )
        self.assertEqual(active.retrieval_config["search_top_k"], 9)
        self.assertEqual(active.generation_config["temperature"], 0.4)

    def test_invalid_or_oversized_instruction_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get(f"{BASE}/")

        empty = self.save_prompt_draft(instruction="")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")
        oversized = self.save_prompt_draft(instruction="x" * 12_001)
        self.assertEqual(oversized.status_code, 400)
