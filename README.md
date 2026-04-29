# Flow Worker

Flow Worker는 `Flow Classic Plus`를 직접 실행하지 않는 새 독립 프로그램입니다.

목표는 이것입니다.

- UI와 구조는 `Grok Worker`처럼 가볍고 단순하게
- 기능은 `Flow` 전용으로 정리
- 안 쓰는 통합 기능은 과감히 빼기
- 새 Codex가 와도 바로 이어받을 수 있게 문서와 Git 이력을 항상 남기기

## 현재 상태

- 새 독립 저장소 / 새 독립 런타임 구조
- 프로젝트 목록 / 프롬프트 파일 / 저장 폴더 관리
- 독립 Edge 실행 및 CDP 연결
- 기본 연결 포트와 브라우저 프로필도 FlowWorker 전용으로 분리
- 이미지 / 비디오 공용 UI
- 프롬프트 번호 선택 계획 생성
- `Flow Classic Plus`의 설정/프롬프트 데이터 1회 가져오기 지원
- 이미지 모드 1차 코어:
  - 브라우저 열기
  - Flow 입력창 찾기
  - 이미지 모드 맞추기
  - 생성 개수 / 품질 맞추기
  - 사람처럼 프롬프트 타이핑
  - `@S001` 같은 inline 레퍼런스 첨부
    - `@` 트리거
    - 레퍼런스 검색창 탐지
    - `오래된 순` 정렬
    - 태그 입력 후 `Enter` 첨부
    - 이어서 본문 타이핑 계속
  - 생성 버튼 제출
  - 생성 후 남은 시간 실시간 안내
  - 이미지 다운로드 자동화 1차

중요:

- 이 저장소는 `Flow Classic Plus` 코드를 import 하거나 실행하지 않습니다.
- 다만 기존 기능을 새 구조로 옮기기 위해, 필요한 로직은 참고 후 재구성해서 이식합니다.

## 실행 방법

Windows에서:

- `FlowWorker_실행.vbs`
- 또는 `python -m flow_worker.main`

검은 CMD 창 없이 여는 기본 실행 파일은 `FlowWorker_실행.vbs`입니다.

## 브라우저 분리 원칙

- `FlowWorker`는 그록워커와 Edge 프로필을 공유하지 않습니다.
- 기본 Edge 프로필 경로: `runtime/flow_worker_edge_profile`
- 기본 디버그 포트: `127.0.0.1:9333`

즉, 그록워커가 다른 포트/프로필을 써도 서로 겹치지 않게 설계합니다.

## 문서

- `codex 설명서.md`
- `CODEX_연속작업_인수인계_20260304.md`
- `새 Codex 시작용 한방 프롬프트.txt`

새 세션에서 이어서 작업할 때는 위 3개 문서를 먼저 보면 됩니다.

## 폴더 구조

- `flow_worker/config.py`
- `flow_worker/prompt_parser.py`
- `flow_worker/browser.py`
- `flow_worker/automation.py`
- `flow_worker/ui.py`
- `flow_worker/assets/`
