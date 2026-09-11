import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from asgiref.sync import async_to_sync

from config.enums import AIStatus, NodeType
from document_ai.models import RAGConversation, RAGJob, RAGMessage
from document_ai.rag.streaming import _create_rag_jobs_sync
from document_ai.services.rag_conversation_service import (
    ConversationIdempotencyConflict,
    append_user_message,
)
from files.models import Node
from workspaces.models import WorkspaceMembership
from workspaces.services import create_team_workspace


User = get_user_model()


class RAGConversationApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="conversation-owner@example.com", password="test-pass",
            email_verified=True, is_active=True,
        )
        self.member = User.objects.create_user(
            email="conversation-member@example.com", password="test-pass",
            email_verified=True, is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.owner, name="Conversation Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER, status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def _login(self, user):
        self.client.force_login(user)
        response = self.client.post(
            "/api/workspaces/v1/switch/",
            data=json.dumps({"workspace_uid": str(self.workspace.uid)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def test_workspace_members_can_read_but_only_owner_or_admin_can_edit(self):
        self._login(self.owner)
        created = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"title": "계약 검토", "default_node_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        conversation = created.json()["conversation"]
        self.assertEqual(conversation["title"], "계약 검토")

        self.client.logout()
        self._login(self.member)
        listed = self.client.get("/api/document-ai/v1/rag/conversations/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["uid"] for item in listed.json()["conversations"]], [conversation["uid"]])
        forbidden = self.client.patch(
            f"/api/document-ai/v1/rag/conversations/{conversation['uid']}/",
            data=json.dumps({"title": "수정", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_message_order_and_idempotency_are_persisted(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="질문"
        )
        request_id = uuid.uuid4()
        first, created = append_user_message(
            conversation=conversation, user=self.owner, content="첫 질문", client_request_id=request_id,
            node_ids=[],
        )
        replay, replay_created = append_user_message(
            conversation=conversation, user=self.owner, content="첫 질문", client_request_id=request_id,
            node_ids=[],
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(RAGMessage.objects.filter(conversation=conversation).count(), 1)

        with self.assertRaises(ConversationIdempotencyConflict):
            append_user_message(
                conversation=conversation, user=self.owner, content="다른 질문",
                client_request_id=request_id, node_ids=[],
            )

    def test_default_scope_requires_live_nodes_in_the_current_workspace(self):
        self._login(self.owner)
        node = Node.objects.create(
            owner=self.owner, workspace=self.workspace, name="scope", ext="",
            node_type=NodeType.FOLDER, path="/scope",
        )
        valid = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"default_node_ids": [str(node.uid)]}),
            content_type="application/json",
        )
        self.assertEqual(valid.status_code, 201)
        invalid = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"default_node_ids": ["not-a-uuid", str(uuid.uuid4())]}),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_DOCUMENT_SCOPE")

    def test_revision_update_rejects_stale_writes(self):
        self._login(self.owner)
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="v1"
        )
        path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/"
        first = self.client.patch(
            path, data=json.dumps({"title": "v2", "expected_revision": 1}),
            content_type="application/json",
        )
        stale = self.client.patch(
            path, data=json.dumps({"title": "stale", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["conversation"]["revision"], 2)
        self.assertEqual(stale.status_code, 409)
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "v2")

    def test_session_stream_reports_idempotency_conflict_separately(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        request_id = uuid.uuid4()
        append_user_message(
            conversation=conversation, user=self.owner, content="원래 질문",
            client_request_id=request_id, node_ids=[],
        )
        self._login(self.owner)
        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ):
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "다른 질문", "language": "ko",
                    "client_request_id": str(request_id),
                }),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        admission_token.release_async.assert_awaited_once()

    def test_rag_job_rejects_a_conversation_from_another_workspace(self):
        other_workspace = create_team_workspace(actor=self.owner, name="Other Team")
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        with self.assertRaisesMessage(ValueError, "same workspace"):
            RAGJob.objects.create(
                workspace=other_workspace, owner=self.owner,
                conversation=conversation, question="잘못된 연결",
            )

    def test_session_job_and_assistant_message_are_atomic_and_restorable(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="세션"
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ):
            _, rag_job = _create_rag_jobs_sync(
                owner=self.owner, workspace=self.workspace, question="질문",
                retrieval_query="질문", top_k=3, threshold=None, language="ko",
                requested_node_ids=[], scoped_node_ids=[], llm_snapshot={},
                conversation=conversation, reply_to=user_message,
            )
        self.assertEqual(rag_job.conversation_id, conversation.id)
        assistant = RAGMessage.objects.get(rag_job=rag_job)
        self.assertEqual(assistant.reply_to_id, user_message.id)
        self.assertEqual(assistant.sequence, 2)

        rag_job.status = AIStatus.COMPLETED
        rag_job.answer = "저장된 답변"
        rag_job.citations = [{"id": 1, "text": "근거"}]
        rag_job.save(update_fields=["status", "answer", "citations", "updated_at"])
        self._login(self.owner)
        restored = self.client.get(
            f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/"
        )
        self.assertEqual(restored.status_code, 200)
        messages = restored.json()["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["rag_job"]["answer"], "저장된 답변")

    def test_session_stream_endpoint_links_and_exposes_message_identity(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="실제 스트림"
        )
        self._login(self.owner)
        request_id = uuid.uuid4()
        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())

        async def generation_events(_job_id):
            yield {"type": "terminal", "result": {"status": "success"}}

        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ), patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ), patch(
            "document_ai.rag.async_generation.iter_rag_generation_events_async",
            side_effect=generation_events,
        ):
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "세션 질문", "language": "ko",
                    "client_request_id": str(request_id),
                }),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

            async def collect_events():
                return [json.loads(chunk.decode("utf-8")) async for chunk in response.streaming_content]

            events = async_to_sync(collect_events)()

        self.assertEqual([event["type"] for event in events], ["started", "sources", "completed"])
        self.assertEqual(events[0]["conversation_uid"], str(conversation.uid))
        self.assertEqual(events[0]["client_request_id"], str(request_id))
        self.assertTrue(events[0]["message_uid"])
        messages = list(RAGMessage.objects.filter(conversation=conversation).order_by("sequence"))
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(str(messages[1].uid), events[0]["message_uid"])
        self.assertEqual(messages[1].rag_job.conversation_id, conversation.id)
        admission_token.release_async.assert_awaited_once()

    def test_job_and_message_link_roll_back_together(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ), patch("document_ai.models.RAGMessage.objects.create", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                _create_rag_jobs_sync(
                    owner=self.owner, workspace=self.workspace, question="질문",
                    retrieval_query="질문", top_k=3, threshold=None, language="ko",
                    requested_node_ids=[], scoped_node_ids=[], llm_snapshot={},
                    conversation=conversation, reply_to=user_message,
                )
        self.assertFalse(RAGJob.objects.filter(conversation=conversation).exists())
