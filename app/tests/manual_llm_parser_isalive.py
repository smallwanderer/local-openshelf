import os
import sys
import django
import time
import requests
import concurrent.futures
from typing import Any

__test__ = False

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from document_ai.tasks import generate_text2sql_response

model = "google/gemma-4-E4B-it"
system_prompt = (
    "당신은 PostgreSQL용 SQL 생성기입니다. "
    "설명, 주석, 코드블록 없이 SQL 쿼리 하나만 출력하세요."
)
prompt = "부서가 'IT'인 직원들의 이름과 급여를 조회하는 SQL을 작성하세요."
api_url = os.getenv("MANUAL_LLM_API_URL", "http://llm-parser:8080/v1/chat/completions")
health_url = os.getenv("MANUAL_LLM_HEALTH_URL", "http://llm-parser:8080/health")


def build_payload(user_prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 128,
        "stream": False,
        "reasoning_format": "none",
    }


def extract_metrics(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage", {})
    timings = result.get("timings", {})
    reply = ""
    choices = result.get("choices", [])
    if choices:
        reply = choices[0].get("message", {}).get("content", "")

    return {
        "reply": reply,
        "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_ms": timings.get("predicted_ms"),
        "total_ms": timings.get("prompt_ms", 0) + timings.get("predicted_ms", 0),
    }


def print_metrics(label: str, elapsed: float, result: dict[str, Any]) -> None:
    metrics = extract_metrics(result)
    print(f"\n[{label}]")
    print(f">> wall_time_s: {elapsed:.2f}")
    print(f">> cached_tokens: {metrics['cached_tokens']}")
    print(f">> prompt_tokens: {metrics['prompt_tokens']}")
    print(f">> completion_tokens: {metrics['completion_tokens']}")
    print(f">> prompt_ms: {metrics['prompt_ms']}")
    print(f">> predicted_ms: {metrics['predicted_ms']}")
    print(f">> total_ms: {metrics['total_ms']}")
    print(f">> reply: {metrics['reply']}")


def direct_request(user_prompt: str) -> tuple[float, dict[str, Any]]:
    start_time = time.time()
    response = requests.post(api_url, json=build_payload(user_prompt), timeout=120)
    response.raise_for_status()
    elapsed = time.time() - start_time
    return elapsed, response.json()


def celery_request(user_prompt: str) -> tuple[float, dict[str, Any]]:
    start_time = time.time()
    async_result = generate_text2sql_response.delay(user_prompt)
    result = async_result.get(timeout=None)
    elapsed = time.time() - start_time
    return elapsed, result


def run_celery_task_single():
    print("--- [1] Celery Task 단일 요청 큐잉 테스트 ---")
    print(f"질문: {prompt}")

    try:
        elapsed, result = celery_request(prompt)
        if "choices" in result:
            print_metrics("Celery Single", elapsed, result)
        else:
            print(f">> 응답 에러: {result}")
    except Exception as e:
        print(f"[FAIL] 요청 실패: {e}")

def run_celery_task_load(concurrent_users=5):
    print(f"\n--- [2] Celery Task 부하 테스트 ({concurrent_users}개 요청 동시 큐잉) ---")
    print(f"큐에 {concurrent_users}개의 작업을 동시에 밀어 넣고, Semaphore가 잘 동작하는지 확인합니다...")
    
    def fetch(user_id):
        start = time.time()
        try:
            # 큐로 작업 전송
            async_result = generate_text2sql_response.delay(f"부서가 'IT'인 직원들의 이름과 급여를 알려주는 쿼리를 짜줘.")
            # 결과 무한 대기
            result = async_result.get(timeout=None)
            elapsed = time.time() - start
            
            if "choices" in result:
                output = result['choices'][0]['message']['content']
                return user_id, True, elapsed, output
            else:
                return user_id, False, elapsed, str(result)
        except Exception as e:
            elapsed = time.time() - start
            return user_id, False, elapsed, str(e)

    start_time = time.time()
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_users) as executor:
        futures = [executor.submit(fetch, i+1) for i in range(concurrent_users)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    total_time = time.time() - start_time
    
    print(f"\n[부하 테스트 완료]")
    print(f">> 전체 소요 시간: {total_time:.2f} 초")
    
    results.sort(key=lambda x: x[0])
    for user_id, success, elapsed, output in results:
        status = "[SUCCESS]" if success else "[FAIL]"
        output_preview = repr(output.replace('\n', ' ')[:40] + ("..." if len(output) > 40 else ""))
        print(f" - 유저 {user_id}: {status} (대기 포함 총 소요: {elapsed:.2f}초) | 응답: {output_preview}")


def run_external_task_single():
    print("단일 요청을 보내 외부에서 API 입출력 과정 확인")

    print("외부 LLM API 연결 테스트")
    try:
        health_res = requests.get(health_url, timeout=5)
        if health_res.status_code == 200:
            print(f"[SUCCESS] Health Check 통과 (코드: 200)")
        else:
            print(f"[FAIL] Health Check 실패 (코드: {health_res.status_code})")
    except Exception as e:
        print(f"[FAIL] Health Check 연결 실패: {e}")

    start_time = time.time()
    print(f">>> POST 요청을 {api_url}로 전송...")
    elapsed, data = direct_request(prompt)

    print("\n" + "#"*20 + " 외부 API 응답 결과 " + "#"*20)
    choices = data.get("choices", [])
    if choices:
        print(f"[SUCCESS] 외부 API 응답 완료 (응답 시간: {elapsed:.2f}초)")
        print_metrics("External Single", elapsed, data)
    else:
        print(f"[FAIL] HTTP 200 OK, but invalid response structure: {data}")


def compare_request_paths(order: list[str] | None = None, user_prompt: str = prompt):
    sequence = order or ["external", "celery", "external", "celery"]

    print("\n--- [3] External vs Celery Warm-Cache Comparison ---")
    print(f"질문: {user_prompt}")
    print(f"순서: {', '.join(sequence)}")

    for index, mode in enumerate(sequence, start=1):
        print(f"\n=== Run {index}: {mode} ===")
        try:
            if mode == "external":
                elapsed, result = direct_request(user_prompt)
            elif mode == "celery":
                elapsed, result = celery_request(user_prompt)
            else:
                raise ValueError(f"Unknown mode: {mode}")

            print_metrics(f"{mode.upper()} RUN {index}", elapsed, result)
        except Exception as e:
            print(f"[FAIL] {mode} run {index}: {e}")


if __name__ == "__main__":
    # run_celery_task_single()
    # run_celery_task_load(concurrent_users=5)
    compare_request_paths()
