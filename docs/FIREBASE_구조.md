# Firebase 구조 (앱 ⇄ 드론 업로더 약속)

앱은 아래 경로만 읽고 씁니다. 드론에 단 라즈베리파이의 업로더는 이 형식대로 올리면 지도에서 칸을 눌렀을 때 바로 보입니다.

## 1. 앱이 쓰는 문서

### `missions/{임무ID}` — 경로 업로드 때 생성
| 필드 | 형식 | 설명 |
|---|---|---|
| status | string | uploaded → flying → done / aborted |
| home | GeoPoint | 출발지 |
| alt, speed, shot | number | 고도(m), 속도(m/s), 사진 간격(m) |
| grid | map | `lat0, lng0, x0, y0, cw, ch, cols, rows` (칸 번호 계산용) |
| drones | array | `{id, name, cells, route: GeoPoint[]}` |
| createdAt, startedAt, endedAt | Timestamp | |
| coverage | number | 종료 시 탐색률 (0~1) |

임무ID 예: `M20261008-173015`

### `fleet/{드론번호}` — 업로드 때 갱신
`{ currentMission: "M20261008-173015", updatedAt }`
→ 업로더는 이 문서를 보고 어느 임무에 사진을 올릴지 정합니다.

## 2. 드론 업로더가 쓰는 문서

### `missions/{임무ID}/photos/{자동ID}`
| 필드 | 형식 | 필수 | 설명 |
|---|---|---|---|
| drone | number | ✔ | 드론 번호 (SYSID와 같게) |
| lat, lng | number | ✔ | 촬영 순간 위치 (아래 "PX4 촬영 위치" 참고) |
| alt | number | | 상대고도 m |
| hdg | number | | 기수 방향 ° |
| takenAt | Timestamp | ✔ | 촬영 시각 |
| cell | number | 권장 | 칸 번호 (아래 공식). 없으면 앱이 위도 범위로 찾음 |
| thumbUrl | string | 권장 | 축소본 다운로드 URL (목록에 사용) |
| url | string | | 원본 다운로드 URL (크게 보기) |
| path | string | | Storage 경로 (url이 없으면 앱이 이 경로로 URL을 받음) |

### `missions/{임무ID}/detections/{자동ID}` — 열화상 탐지 (34°C 이상)
앱은 비행 중 이 컬렉션을 **실시간 구독**합니다. 문서가 추가되는 즉시 지도에 빨간 점·알림이 뜨고 그 칸이 깜박입니다.

| 필드 | 형식 | 필수 | 설명 |
|---|---|---|---|
| drone | number | ✔ | 드론 번호 (MAV_SYS_ID) |
| lat, lng | number | ✔ | 열점 위치 (아래 "열점 위치 계산" 참고) |
| tempC | number | ✔ | 열점 최고 온도 °C. 앱의 기준 온도(missions 문서의 `thermalMinC`, 기본 34) 미만은 표시 안 함 |
| takenAt | Timestamp | ✔ | 촬영 시각 |
| alt, hdg | number | | 촬영 당시 상대고도·방향 |
| cell | number | 권장 | 칸 번호 (아래 공식) |
| thumbUrl / url / path | string | 권장 | 열화상 이미지 (축소본 / 원본 / Storage 경로) |
| conf | number | | 판별 신뢰도 % (사람 형태 판별을 쓴다면) |

- 같은 대상을 여러 장에서 잡으면 문서가 여러 개 생깁니다. 파이 쪽에서 반경 10m·30초 안의 중복은 하나로 합쳐 올리는 것을 권장합니다.
- 앱 판정: `tempC ≥ 45`(설정에서 변경 가능) → 고온 열원(화재 의심), 그 아래 → 인명 추정(체온대).
- 열화상 카메라의 표면 온도는 거리·습도·방사율 때문에 실제 체온보다 낮게 나오는 경우가 많습니다. 34°C 기준은 현장 시험으로 조정하세요.

### 열점 위치 계산
드론 바로 아래가 아닌 화면 가장자리의 열점은 드론 위치와 수십 m 차이가 납니다. 카메라가 수직(짐벌 -90°)이라면:
```
지상 폭 W = 2 · alt · tan(가로화각/2),  지상 높이 H = 2 · alt · tan(세로화각/2)
dx = (px/이미지폭 - 0.5) · W     # 오른쪽 +
dy = (0.5 - py/이미지높이) · H   # 앞쪽 +
기수 방향 hdg만큼 회전 → 동(E)·북(N) 오프셋 → 위도·경도에 더함
```

### PX4 촬영 위치 (라즈베리파이)
- PX4 파라미터 `TRIG_INTERFACE = MAVLink`, `TRIG_MODE = Distance (on command)` 로 두면, 앱이 올린 임무의 `DO_SET_CAM_TRIGG_DIST` 간격마다 PX4가 `CAMERA_TRIGGER` 메시지를 보냅니다.
- 파이는 PX4 TELEM2에 UART로 연결해 이 메시지를 받는 순간 사진을 찍고, 그때의 `GLOBAL_POSITION_INT`(위도·경도·상대고도·방향)를 함께 기록합니다.
- 파이가 mavlink-router로 텔레메트리를 지상 브리지에도 넘겨주면, 촬영과 위치 전달을 같은 연결로 처리할 수 있습니다.

### 칸 번호(cell) 공식
`missions/{임무ID}.grid` 값으로 계산합니다.
```
x = (lng - lng0) * 111320 * cos(lat0 * π/180)
y = (lat - lat0) * 111320
c = floor((x - x0) / cw)      # 0 ~ cols-1
r = floor((y - y0) / ch)      # 0 ~ rows-1  (남쪽이 0)
cell = r * cols + c           # 범위 밖이면 cell을 넣지 않음
```

### Storage 경로 (권장)
```
missions/{임무ID}/{드론번호}/{촬영시각}_{순번}.jpg          원본
missions/{임무ID}/{드론번호}/thumb/{촬영시각}_{순번}.jpg    축소본 (긴 변 1280px, ~200KB)
```

## 3. 보안 규칙 예시 (Firestore)
드론 계정에는 Admin SDK로 커스텀 클레임 `{drone: true, droneId: 1}`을 붙여 둡니다.
```
rules_version = '2';
service cloud.firestore {
  match /databases/{db}/documents {
    function signedIn() { return request.auth != null; }
    function isDrone()  { return signedIn() && request.auth.token.drone == true; }
    match /missions/{mid} {
      allow read: if signedIn();
      allow write: if signedIn() && !isDrone();
      match /photos/{pid} {
        allow read: if signedIn();
        allow create: if isDrone() && request.resource.data.drone == request.auth.token.droneId;
        allow update, delete: if false;
      }
    }
    match /missions/{mid}/detections/{did} {
      allow read: if signedIn();
      allow create: if isDrone() && request.resource.data.drone == request.auth.token.droneId
                    && request.resource.data.tempC is number;
      allow update, delete: if false;
    }
    match /fleet/{id} {
      allow read: if signedIn();
      allow write: if signedIn() && !isDrone();
    }
  }
}
```

## 4. 앱에서 연결
설정 → Firebase (사진 서버) → firebaseConfig 붙여 넣기 → 이메일·비밀번호 → 연결 · 로그인.
Authentication에서 이메일/비밀번호 로그인을 켜 두어야 합니다.
