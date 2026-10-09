# 드론 수색 GCS — 안드로이드 패드 앱

산불·인명 수색 드론의 구역 분할, 경로 업로드(PX4), 실시간 위치·탐색률, 열화상 탐지 알림을 보여 주는 관제 앱입니다.

```
web/        앱 화면 (HTML 하나). APK에도, 웹(PWA)에도 이 폴더를 그대로 씀
app/        안드로이드 앱 (web/ 를 감싸는 WebView + 위치 권한·파일 저장·화면 켜짐·진동)
bridge/     gcs_bridge.py — PX4 드론 ⇄ 앱 연결 (지상 노트북 또는 패드 Termux에서 실행)
docs/       Firebase 구조 (라즈베리파이 업로더가 따를 형식)
```

---

## 1. APK 만들기

### 방법 A — GitHub에서 자동으로 (PC에 아무것도 설치하지 않아도 됨)

1. github.com 에서 새 저장소를 만듭니다 (비공개 권장).
2. 이 폴더 전체를 올립니다. 웹에서 올릴 때는 **Add file → Upload files** 로 폴더 안 파일을 모두 끌어다 놓으세요.
   `.github` 폴더도 꼭 함께 올라가야 합니다.
   점(.)으로 시작하는 폴더는 컴퓨터에서 숨김 처리되어 빠질 때가 있습니다. 올린 뒤 저장소에 `.github` 가 없으면:
   **Actions** 탭 → **set up a workflow yourself** → `.github/workflows/build-apk.yml` 내용을 그대로 붙여 넣고 **Commit**.
3. 저장소의 **Actions** 탭 → **APK 빌드** 가 자동으로 돌아갑니다 (약 3~5분).
4. 끝난 실행을 눌러 맨 아래 **Artifacts → drone-gcs-apk** 를 내려받으면 압축 안에 `app-debug.apk` 가 있습니다.

### 방법 B — Android Studio

1. Android Studio → **Open** → 이 폴더 선택 (처음 열 때 필요한 SDK를 자동으로 받습니다).
2. 메뉴 **Build → Build App Bundle(s) / APK(s) → Build APK(s)**.
3. `app/build/outputs/apk/debug/app-debug.apk` 가 만들어집니다.

## 2. 패드에 설치

1. APK를 패드로 옮깁니다 (USB, 구글 드라이브, 카카오톡 나에게 보내기 등).
2. 패드의 **파일** 앱에서 APK를 누릅니다.
3. "출처를 알 수 없는 앱" 안내가 나오면 **설정 → 이 출처 허용** 을 켜고 돌아와 **설치**.
4. 처음 실행하면 위치 권한을 묻습니다 → **앱 사용 중에만 허용**.

> 지금 APK는 디버그 서명입니다. 업데이트할 때 같은 방법으로 만든 APK를 덮어 설치하면 설정과 임무 이력이 유지됩니다.
> 다른 PC에서 빌드한 APK는 서명이 달라 덮어 설치가 안 되니, 그때는 기존 앱을 지우고 설치하세요.

## 3. 앱에서 달라지는 점 (브라우저와 비교)

| 기능 | 브라우저(PWA) | 안드로이드 앱 |
|---|---|---|
| 위치 | HTTPS 주소에 올려야 동작 | 바로 동작 (앱이 권한을 직접 받음) |
| 브리지 연결 | 다른 기기면 `wss://` + 인증서 필요 | 같은 Wi-Fi 노트북에 `ws://<IP>:8766` 바로 연결 |
| CSV·GeoJSON 저장 | 브라우저 다운로드 | `다운로드/DroneGCS` 폴더 |
| 화면 꺼짐 방지 | 지원되는 브라우저에서만 | 항상 |
| 뒤로 가기 | 브라우저 뒤로 | 사진 → 시트 → 다른 메뉴 순으로 닫고, 지도에서는 두 번 눌러 종료 |
| 위치 권한 거부 시 | 안내 문구 | ‘앱 설정 열기’ 버튼으로 바로 이동 |

인터넷이 필요한 것: 지도 타일(한 번 본 지역은 저장되어 오프라인에서도 보임), Firebase.
인터넷 없이 되는 것: 앱 화면, 구역 계산, 경로 업로드·비행 추적(브리지와 같은 Wi-Fi만 있으면 됨).

## 4. 브리지 (PX4 드론 연결)

`bridge/gcs_bridge.py` 맨 위 설명을 참고하세요. 요약:

```
pip install pymavlink
python3 bridge/gcs_bridge.py --mav udpin:0.0.0.0:14550        # 실기체
python3 bridge/gcs_bridge.py --demo --lat 33.4996 --lng 126.5312   # 시험용 데모 드론
```
앱 → 설정 → 드론 실시간 위치 → **실시간** → 주소 `ws://<노트북 IP>:8766` → **연결**.

## 5. Firebase 사용 시 확인

- API 키에 웹사이트 제한을 걸었다면 `https://appassets.androidplatform.net` 를 허용 목록에 추가하세요 (앱이 이 주소로 동작합니다).
- 이메일/비밀번호 로그인을 쓰려면 Firebase 콘솔 → Authentication 에서 켜 두세요.
