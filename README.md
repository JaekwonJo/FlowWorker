# Flow Worker

`Flow Worker`는 새 독립 프로그램입니다.

이 프로젝트의 기준은 아주 단순합니다.

- `Flow Classic Plus`의 `이미지 워커`
- `Flow Classic Plus`의 `S자동화 워커`

오직 이 두 개만 참고해서, 두 워커를 가볍게 합친 새 앱을 만드는 것이 목적입니다.

## 현재 상태

- 독립 저장소와 독립 실행 구조는 남겨둔 상태입니다.
- 넓게 베껴왔던 중간 이식 코드는 되돌리는 방향으로 정리 중입니다.
- 이제부터는 `이미지 워커 / S자동화 워커`만 기준으로 다시 합칩니다.

## 실행

Windows:

- `FlowWorker_실행.vbs`
- 또는 `python -m flow_worker.main`

## 현재 폴더 구성

- `flow_worker/config.py`
- `flow_worker/prompt_parser.py`
- `flow_worker/browser.py`
- `flow_worker/automation.py`
- `flow_worker/ui.py`

## 개발 원칙

- `Flow Classic Plus 전체`를 기준으로 삼지 않습니다.
- 메인 통합 앱, 이어달리기, 기타 안 쓰는 기능은 참고 대상이 아닙니다.
- 새 기능을 넣을 때는 먼저 `이미지 워커`와 `S자동화 워커` 안에서 실제 동작을 확인한 뒤 옮깁니다.

## 현재 반영한 프롬프트 규칙

- 이미지 프롬프트:
  - `S001 Prompt : ...`
  - `S001>S002 Prompt : ...`
  - 실제 Flow 입력은 둘 다 `S001 Prompt : ...` 형태로 정리합니다.
- 비디오 프롬프트:
  - `V001 Prompt : ...`
  - `V001>V002 Prompt : ...`
  - 실제 Flow 입력은 `V001 Prompt : ...` 형태로 정리합니다.
  - 대신 라우트 정보는 따로 보존해서, 나중에 시작 프레임 `S001`, 끝 프레임 `S002`처럼 연결할 수 있게 준비합니다.
