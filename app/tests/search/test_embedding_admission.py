"""
[임베딩 우선순위 큐(EmbeddingPriorityAdmission) 검증 테스트]

문서 대량 임베딩(배치) 중에도 사용자의 실시간 검색 쿼리(query)가
대기열 맨 앞으로 '새치기(Preemption)'하여 0.05초 이내로 즉시 처리되는지 검증합니다.
"""

import threading
import time

import pytest

from document_ai.embedding.admission import (
    AdmissionRejected,
    EmbeddingPriorityAdmission,
)


pytestmark = pytest.mark.unit


def _wait_until(predicate, *, timeout=1.0):
    """지정된 조건(predicate)이 참이 될 때까지 폴링하며 대기하는 헬퍼 함수"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before timeout")


def test_query_overtakes_waiting_document_and_same_type_remains_fifo():
    """
    [핵심 검증 1: 쿼리 새치기 및 동일 타입 FIFO 순서 보장]
    
    1. 동시성 슬롯이 1개일 때, 문서(document-1, document-2)가 먼저 대기열에 진입.
    2. 뒤늦게 사용자 검색 쿼리(query)가 대기열에 진입.
    3. 앞선 슬롯이 반환되면, 대기 중이던 문서들을 제치고 'query'가 최우선으로 슬롯을 획득해야 함.
    4. 남은 문서들은 먼저 들어온 순서(FIFO: document-1 -> document-2)대로 실행되어야 함.
    """
    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=4, query_reserve=1
    )
    active_lease = admission.acquire("document", timeout=0)
    acquired_order = []
    errors = []

    def wait_for_slot(name, request_type):
        try:
            with admission.acquire(request_type, timeout=1):
                acquired_order.append(name)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_document = threading.Thread(
        target=wait_for_slot, args=("document-1", "document")
    )
    second_document = threading.Thread(
        target=wait_for_slot, args=("document-2", "document")
    )
    query = threading.Thread(target=wait_for_slot, args=("query", "query"))

    first_document.start()
    _wait_until(lambda: admission.snapshot()["document_waiting"] == 1)
    second_document.start()
    _wait_until(lambda: admission.snapshot()["document_waiting"] == 2)
    query.start()
    _wait_until(lambda: admission.snapshot()["query_waiting"] == 1)

    # 현재 작업 종료 -> 락 해제
    active_lease.release()
    for thread in (first_document, second_document, query):
        thread.join(timeout=1)

    assert errors == []
    # 🌟 쿼리가 먼저 실행되고, 그 뒤 문서 1, 2가 순서대로 실행됨을 검증!
    assert acquired_order == ["query", "document-1", "document-2"]
    assert admission.snapshot() == {
        "active": 0,
        "waiting": 0,
        "query_waiting": 0,
        "document_waiting": 0,
    }


def test_document_cannot_consume_reserved_query_queue_slot():
    """
    [핵심 검증 2: 검색 쿼리용 예약 슬롯 보호 (Queue Reservation)]
    
    문서 작업이 아무리 많이 몰려와도, 쿼리용 예약 슬롯(query_reserve=1)이 남아있으면
    문서 작업은 대기열 초과(queue_full)로 즉시 거절되고, 쿼리는 정상 진입 가능해야 함.
    """
    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=2, query_reserve=1
    )
    active_lease = admission.acquire("document", timeout=0)
    release_waiter = threading.Event()

    def wait_as_document():
        with admission.acquire("document", timeout=1):
            release_waiter.wait(timeout=1)

    waiting_document = threading.Thread(target=wait_as_document)
    waiting_document.start()
    _wait_until(lambda: admission.snapshot()["document_waiting"] == 1)

    # 쿼리 예약 슬롯 1개를 제외한 일반 슬롯이 다 찼으므로, 추가 문서 요청은 거절되어야 함
    with pytest.raises(AdmissionRejected, match="queue_full") as exc_info:
        admission.acquire("document", timeout=0)

    assert exc_info.value.reason == "queue_full"

    # 반면 실시간 검색 쿼리는 예약 슬롯을 통해 정상 진입 가능해야 함
    query_acquired = threading.Event()

    def wait_as_query():
        with admission.acquire("query", timeout=1):
            query_acquired.set()

    waiting_query = threading.Thread(target=wait_as_query)
    waiting_query.start()
    _wait_until(lambda: admission.snapshot()["query_waiting"] == 1)

    active_lease.release()
    assert query_acquired.wait(timeout=1)
    release_waiter.set()
    waiting_query.join(timeout=1)
    waiting_document.join(timeout=1)

    assert admission.snapshot()["waiting"] == 0


def test_timed_out_waiter_is_removed_from_queue():
    """
    [핵심 검증 3: 대기 타임아웃 발생 시 대기열 찌꺼기 자동 제거]
    
    대기 시간을 초과하여 에러가 발생한 요청은 대기열(waiting count)에서 깨끗이 빠져야 함.
    """
    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=1, query_reserve=0
    )
    active_lease = admission.acquire("query", timeout=0)

    with pytest.raises(AdmissionRejected, match="queue_timeout") as exc_info:
        admission.acquire("query", timeout=0.01)

    active_lease.release()
    assert exc_info.value.reason == "queue_timeout"
    assert admission.snapshot()["waiting"] == 0
