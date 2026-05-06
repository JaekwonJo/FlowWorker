# Afterglow Groove 기획서

## 프로젝트 이름
- 영문명: `Afterglow Groove`
- 한국어명: `애프터글로우 그루브`
- 총괄 프로듀서 별칭: `그루브PD`

## 한 줄 정의
70s/80s 영미 그루브 감성의 원곡과, 빈티지 필름 단편 뮤직비디오를 결합한 음악 채널.

## 역할 구조
- 사용자: 방향 제시자, 취향 결정자, 최종 승인자
- 그루브PD: 총괄 프로듀서
- FlowWorker: 실행 직원

사용자는 “이런 느낌의 음악 만들고 싶어요”라고 말한다.
그루브PD는 그 방향을 받아 아래 산출물을 직접 만든다.
- 곡 콘셉트
- Suno 프롬프트
- 가사
- 뮤직비디오 플롯
- FlowWorker용 이미지/비디오 프롬프트
- 업로드/라이브 운영안

## 핵심 시청자 반응
```text
오, 이 채널 음악 좋은데?
근데 영상도 그냥 틀어놓고 볼만하네.
명작 단편 뮤직비디오 영화 같은 느낌이 있네.
작업할 때 계속 틀어놔도 부담이 없네.
```

## 음악 방향
- 70s groovy pop/funk
- 80s soft city pop
- flat melody
- no climax
- no belting
- soft falsetto
- light and smooth male vocal
- rhythmic and groovy delivery
- slightly nasal but warm voice
- relaxed but precise timing
- catchy and minimal phrasing

## 영상 방향
샘플 기준:
- `Sample4`: 어두운 아파트, 문밖에서 보는 관찰자 시점, 누아르 감성
- `Sample5`: 아날로그 드로잉, 스토리보드, 기억의 스케치
- `Sample6`: 흑백 패션 필름, 강한 인물 실루엣, 70s/80s 팝 아이콘
- `Sample7`: 빈티지 자동차 내부, 붉은 벨벳 시트, 밤의 여운

영상 키워드:
- vintage cinematic music video
- 1970s/1980s film grain
- urban night
- apartment groove
- old car interior
- dim bar
- rain on window
- neon reflected on glass
- fashion film
- analog storyboard insert
- lonely but stylish

## 서사 원칙
- 영상은 가사를 1:1로 번역하지 않는다.
- 가사는 음악의 정서적 배경일 뿐이다.
- 뮤직비디오는 그 자체로 서사, 스토리, 플롯이 있는 단편 영화처럼 간다.
- 시청자는 가사를 몰라도 영상의 분위기와 사건 흐름을 느낄 수 있어야 한다.

## 컷 연결 원칙
- 마지막 프레임을 뽑아 다음 시작 프레임으로 이어가는 원칙은 유지한다.
- 단, 모든 컷을 무조건 이어붙이지 않는다.
- 장면 전환이 필요한 순간에는 새 S컷으로 끊고 새로운 장소/시간/상태로 넘어간다.
- 즉, 제작 방식은 아래 두 가지를 섞는다.

```text
연속 컷: V001 마지막 프레임 -> S002 -> V002
새 장면 컷: V003 종료 -> 새 이미지 S004 생성 -> V004
```

연속 컷이 좋은 경우:
- 같은 인물이 같은 공간에서 감정이 이어질 때
- 자동차 안, 복도, 바 테이블처럼 움직임의 여운이 중요한 장면
- 음악의 그루브를 끊지 않고 몰입감을 유지해야 할 때

새 장면 컷이 좋은 경우:
- 후렴 없이도 시각적 환기가 필요할 때
- 다른 시간대나 장소로 넘어갈 때
- 기억/회상/상상/스토리보드 컷으로 전환할 때
- 같은 화면이 반복되어 AI 템플릿처럼 보일 위험이 있을 때

## 채널 정체성
음악이 주인공이다. 영상은 음악을 더 오래 듣게 만드는 영화적 표면이다.

반드시 피할 것:
- 같은 구조가 반복되는 포맷
- 같은 배경을 복붙한 영상
- 가사 그대로 시각화하는 저품질 뮤비
- 대량생산 템플릿처럼 보이는 제목/썸네일/설명

반드시 남길 것:
- 곡별 콘셉트
- 제작 의도
- 영상별 색감/장소/감정 차이
- Suno 프롬프트 기록
- FlowWorker 프롬프트 기록
- 사람이 편집하고 선별했다는 제작 로그

## 첫 10편 방향
1. After Work Apartment Groove
2. Midnight Velvet Drive
3. Payphone After Rain
4. Neon Diner Falsetto
5. Last Train Soft Funk
6. Motel Sign Memory
7. Rooftop Radio Love
8. Blue Hallway Dance
9. Red Car Seat Confession
10. Analog Storyboard Girl

## 운영 원칙
- 제작은 FlowWorker로만 실행한다.
- Codex/그루브PD는 Flow 사이트에서 반복 생성하지 않는다.
- 곡마다 영상 문법을 다르게 설계한다.
- 라이브는 가능하지만 스팸처럼 보이면 안 된다.
- 라이브/업로드/쇼츠는 모두 원곡과 편집물의 차별성이 있어야 한다.
