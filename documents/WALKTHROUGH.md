# Dotori for Documents을 안내합니다.
**Open Source Lightweight AI Platform for Document Analysis and Search**

## Dotori 소개

Dotori는 당신과 당신의 그룹이 로컬 LLM에 쉽게 다가갈 수 있도록 구성되어 있습니다. Dotori for Document는 문서 관리에 특화되어 있으며 스마트한 검색, RAG 기능을 제공하는 AI 문서 어시스턴트입니다. 사용자의 로컬 환경에서 인터넷 연결 또는 별도의 비용 추가 없이 문서와 검색, 질의응답 기능을 안전하게 사용할 수 있습니다.

<p align="center">
  <img src="https://github.com/user-attachments/assets/54c7a4a6-39cd-49f9-b5ad-99fe4b24a438" width="80%" alt="Dotori document workspace">
</p>

Dotori는 매우 가볍습니다. 서버를 만드는 데에 엄청난 시스템 자원을 요구하지 않으며, 저사양 컴퓨터에서도 충분히 실행할 수 있습니다. 도커 환경으로 구성되어 윈도우, 맥, 리눅스 모든 운영체제를 지원합니다.

Dotori는 웹 UI뿐만 아니라 CLI와 HTTP API로도 확장됩니다. 서버를 한 번 만들어두면 어떤 클라이언트에서든 같은 문서·검색·RAG 기능을 그대로 이용할 수 있습니다.

## 핵심 기능

- 계정별로 안전하게 분리된 파일·문서 공간
- 인증, 휴지통, 즐겨찾기, 최근 문서를 갖춘 폴더·파일 관리
- PDF, HWP, DOCX 등 텍스트 기반 문서 형식 분석
- 자연어 문서 검색
- 문서 내용만으로 답하는 로컬 RAG
- 서버 하드웨어에 맞춘 로컬 LLM 설치 안내
- 파일 관리만 하는 모드부터 전체 로컬 RAG 스택까지, 세 가지 설치 모드
- 한국어·영어 웹 인터페이스, 외부 AI 모델 연동 지원

## 왜 Dotori인가

- **로컬 우선** — 로컬 LLM으로 운영하면 문서 내용이 외부로 나가지 않습니다. 단, 운영자가 ChatGPT·Claude 같은 외부 모델을 직접 선택하면 그 순간부터 문서 내용이 해당 제공자에게 전송됩니다 — 이 선택은 항상 서버 전체 설정이며 사용자별로 바뀌지 않습니다.
- **가벼움** — 저사양 서버에서도 실행되도록 설계되어, 개인 PC나 NAS에도 그대로 올릴 수 있습니다.
- **여러 인터페이스** — 웹 UI, CLI, HTTP API가 같은 서버 기능을 공유하므로 어느 쪽으로 접속해도 동일한 문서·검색 결과를 얻습니다.

## 처음 설치한다면

- [설치 가이드](./installation-guide.md) — Docker Compose 기반 서버 설치와 세 가지 설치 모드
- [LLM 설치 가이드](./llm-installation-guide.md) — 서버 하드웨어에 맞는 로컬 LLM 선택과 설치

## 이미 운영 중이라면

- [운영 가이드](./operation-guide.md) — 계정·권한 관리, 문서 파이프라인 운용, 검색·RAG 파라미터 조정
- [임베딩 런타임 가이드](./embedding-guide.md) — 임베딩 모델 전환, 외부 엔드포인트 연결, 기존 문서 재임베딩
- [임베딩 설치 가이드](./embedding-installation-guide.md) — 카탈로그에 없는 임베딩 모델을 직접 등록하기
- [임베딩 설치 가이드](./embedding-installation-guide.md) — 원하는 임베딩 모델을 카탈로그에 등록하기

## 상태와 품질을 확인하고 싶다면

- [품질 관리와 평가, 그리고 모니터링 가이드](./monitoring-and-quality-guide.md) — trace_id로 요청 추적, 로그 조회, 검색 품질(Recall/nDCG) 점검

## 직접 연동한다면

- [API 계약](./api-contract-v1.md) — SPA와 CLI가 사용하는 HTTP API 전체 명세
- [Dotori CLI](../clients/dotori-cli/README.md) — 터미널에서 업로드·검색·RAG 질의
- [Dotori Sync](../clients/dotori-sync/README.md) — 로컬 폴더를 워크스페이스와 동기화
