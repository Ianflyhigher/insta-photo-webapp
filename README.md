# 인스타 사진 다운로더

공개 인스타그램 계정의 사진을 **12개씩 목록**으로 보고, 원하는 것만 골라
**원본 화질**로 받거나 **선택한 것만 ZIP**으로 받는 웹앱입니다.
집 PC에서 켜서 같은 와이파이의 폰으로 쓰거나, Render.com 무료로 올려 링크로 쓸 수 있습니다.

## 집 PC에서 실행
1. [Python 설치](https://www.python.org/downloads/) — 설치 첫 화면에서 **Add python.exe to PATH** 체크
2. `실행하기.bat` 더블클릭
3. 폰 브라우저에서 검은 창에 나온 `http://192.168.x.x:5000` 접속

## Render.com 에 배포
| 항목 | 값 |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1 --threads 8` |
| 환경변수 (권장) | `ACCESS_CODE` 접속 암호, `INSTA_SESSIONID` 인스타 sessionid |

`render.yaml` 을 읽는 Blueprint 방식이면 위 설정이 자동으로 들어갑니다.
자세한 순서는 **사용설명서.txt** 의 7번을 보세요.

## 특징
- 목록은 12개씩, `다음 12개 더 불러오기` 로 필요할 때만 추가 요청
- 다운로드 이력(`data/history.json`, shortcode 기준) → **다운 완료** 표시, 중복 다운로드 방지
- 요청 사이 1.5~3초 무작위 대기 · 목록 캐시 · 10초 쿨다운으로 차단 위험 최소화
- 인스타가 로그인을 요구할 때를 위한 **아이디·비밀번호 로그인** (비밀번호는 저장하지 않고, 새 기기 인증 코드 단계까지 화면에서 처리)
- 안 될 때를 위한 예비 수단 **sessionid 입력** (서버 메모리에만 보관, 화면엔 끝 4자리만)
- `ACCESS_CODE` 를 설정하면 접속 암호 화면이 생깁니다

## 주의
공개 계정만 지원합니다. 받은 사진의 저작권·초상권은 올린 사람에게 있으니 개인 보관용으로만 쓰세요.
`session-*` 파일과 `sessionid` 값은 절대 공개 저장소에 올리지 마세요.
