import time
import requests
import concurrent.futures
import json
import sys

# Docker-compose에 설정된 llama-cpp 외부 포트
API_URL = "http://localhost:8081/v1/chat/completions"
HEALTH_URL = "http://localhost:8081/health"

def check_health():
    """서버가 살아있는지 확인합니다."""
    print("--- [0] 헬스 체크 (서버 상태 확인) ---")
    try:
        res = requests.get(HEALTH_URL, timeout=5)
        if res.status_code == 200:
            print("[OK] 서버가 정상적으로 동작 중입니다.\n")
            return True
        else:
            print(f"[FAIL] 서버 응답 에러: {res.status_code}\n")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 서버에 연결할 수 없습니다. (컨테이너가 켜져 있는지 확인하세요)\n에러: {e}")
        return False

def test_single_request():
    """단일 요청을 보내고 API 입출력 과정을 상세히 보여줍니다."""
    print("--- [1] 단일 API 요청 테스트 (통신 과정 확인) ---")
    payload = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply in Korean."},
            {"role": "user", "content": "안녕? 너는 어떤 역할을 할 수 있어? 한 줄로 대답해줘."}
        ],
        "temperature": 0.7,
        "max_tokens": 100
    }
    
    print(f"전송 주소(URL): {API_URL}")
    # print(f"전송 데이터(Payload):\n{json.dumps(payload, indent=2, ensure_ascii=False)}")
    
    print("\n답변을 기다리는 중...")
    start_time = time.time()
    try:
        response = requests.post(API_URL, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        print(len(data["choices"]))
        end_time = time.time()
        
        latency = end_time - start_time
        reply = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        
        print("\n[요청 성공! 결과]")
        print(f">> 모델 답변: {reply}")
        print(f">> 소요 시간: {latency:.2f} 초")
        print(f">> 토큰 사용량: {usage}")
        
        if "total_tokens" in usage:
            tps = usage["total_tokens"] / latency
            print(f">> 처리 속도: {tps:.2f} tokens/sec")
            
    except Exception as e:
        print(f"[FAIL] 요청 실패: {e}")

def test_load(concurrent_users=3):
    """동시 다발적인 요청을 보내 시스템의 부하 처리 능력을 확인합니다."""
    print(f"\n--- [2] 부하 테스트 ({concurrent_users}개 요청 동시 전송) ---")
    print(f"{concurrent_users}명의 유저가 동시에 짧은 이야기를 써달라고 요청합니다...")
    
    payload = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply in Korean."},
            {"role": "user", "content": "고양이에 대한 짧은 문장 2개를 써주세요."}
        ],
        "max_tokens": 50
    }
    
    def fetch(user_id):
        start = time.time()
        try:
            res = requests.post(API_URL, json=payload, timeout=120)
            res.raise_for_status()
            elapsed = time.time() - start
            return user_id, True, elapsed, res.json()["choices"][0]["message"]["content"]
        except Exception as e:
            elapsed = time.time() - start
            return user_id, False, elapsed, str(e)

    start_time = time.time()
    results = []
    
    # ThreadPool을 사용해 동시에 API에 요청을 쏩니다
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrent_users) as executor:
        futures = [executor.submit(fetch, i+1) for i in range(concurrent_users)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    total_time = time.time() - start_time
    
    print(f"\n[부하 테스트 완료]")
    print(f">> 전체 소요 시간: {total_time:.2f} 초")
    
    # 결과 정렬 및 출력
    results.sort(key=lambda x: x[0])
    for user_id, success, elapsed, output in results:
        status = "[SUCCESS]" if success else "[FAIL]"
        output_preview = repr(output.replace('\n', ' ')[:40] + ("..." if len(output) > 40 else ""))
        print(f" - 유저 {user_id}: {status} (소요: {elapsed:.2f}초) | 응답: {output_preview}")

if __name__ == "__main__":
    if not check_health():
        sys.exit(1)
        
    test_single_request()
    test_load(concurrent_users=3) # 동시 접속자 수 조절 가능
