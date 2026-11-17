# 라즈베리파이 설치 (드론 탑재)

직접 만드신 설치 과정(`cam_test.py` 영상, `mavproxy` 중계)을 그대로 이어서, 앱과 바로 붙도록 만든 프로그램입니다.
파이를 켜면 아래 세 가지가 자동으로 돌아갑니다.

| 서비스 | 하는 일 | 앞에서 하던 것 |
|---|---|---|
| `drone-mavproxy` | 픽스호크(`/dev/serial0`, 921600) → 노트북 QGroundControl + 브리지 + 카메라로 나눔 | `mavproxy.py --master=/dev/serial0 ... --out=udp:10.14.61.172:14550` |
| `drone-bridge` | 앱과 통신: 위치·배터리 보내기, 경로 업로드·이륙·복귀 명령, 사진·탐지 전달 (`bridge/gcs_bridge.py`) | (새로 추가) |
| `drone-camera` | 실시간 영상 `:8080/video` + 임무 중 사진 촬영 + 열화상 탐지 (`pi/pi_camera.py`) | `cam_test.py` |

```
픽스호크 ─ /dev/serial0 ─ mavproxy ─┬─ udp:10.14.61.172:14550 → 노트북 QGroundControl
                                   ├─ udp:127.0.0.1:14560    → 브리지 (ws://dmdpi.local:8766 → 태블릿 앱)
                                   └─ udp:127.0.0.1:14561    → 카메라 (위치·모드 → 찍을 때 판단)
카메라 → http://dmdpi.local:8080/video (앱 지도의 '영상' 버튼)
```

## 1. 파일 옮기기

PC에서 `drone-gcs` 폴더 전체를 파이로 복사합니다. VS Code 원격 접속 중이면 왼쪽 탐색기에 폴더를 끌어다 놓아도 됩니다.
```bash
scp -r drone-gcs dmdpi@dmdpi.local:~/
```

## 2. 설치 (한 번만)

```bash
ssh dmdpi@dmdpi.local
cd ~/drone-gcs/pi
nano config.env          # 노트북 IP(QGC_OUT), 앱 비밀번호(TOKEN), 열화상 화각 확인
bash install.sh
sudo reboot
```
- `config.env`의 `MODE`: **`laptop`**(드론 ─ 텔레메트리 ─ 노트북 ─ Wi-Fi ─ 태블릿, 기본값) 또는 `wifi`(태블릿이 파이에 바로). `laptop`이면 파이의 `drone-bridge`는 켜지 않습니다.
- 재부팅 뒤 점검: `bash ~/drone-gcs/pi/check.sh` — 서비스, 시리얼, 픽스호크 하트비트, 카메라, 열화상, 저장 공간을 차례로 확인하고 ❌ 항목에 할 일을 알려 줍니다.
- 앞에서 손으로 켜던 `cam_test.py`, `mavproxy.py`는 **같이 켜지 마세요.** 같은 시리얼 포트와 8080 포트를 서로 쓰려고 다툽니다.
- 설치 스크립트가 하는 일: `mavproxy`·`pymavlink`·OpenCV 설치, 시리얼 켜기·로그인 셸 끄기(직접 하신 `raspi-config` 두 줄과 같음), I2C 켜기(열화상), 자동 실행 서비스 3개 등록.
- CSI 카메라는 `libcamerify`를 붙여 자동으로 실행합니다 (`libcamerify python3 cam_test.py`와 같은 방식).

## 3. 확인

```bash
systemctl status drone-mavproxy drone-bridge drone-camera     # 셋 다 active (running)
journalctl -u drone-camera -f                                 # 카메라 로그
journalctl -u drone-bridge -f                                 # 브리지 로그 ("[MAVLink] 기체 1 연결" 이 나오면 정상)
```
- 브라우저에서 `http://dmdpi.local:8080/video` → 카메라 영상 (`cam_test.py`와 같음)
- QGroundControl은 전처럼 노트북에서 그대로 연결됩니다.

## 4. 앱 연결

설정 → 드론 실시간 위치
- 실시간 · 연결 방식 **브리지 · Wi-Fi**
- 브리지 주소 `ws://dmdpi.local:8766`, 비밀번호 = `config.env`의 `TOKEN` → **연결**
- 드론 영상 주소는 비워 두면 자동(`http://dmdpi.local:8080/video`)

지도 왼쪽 버튼 줄의 **카메라 모양 버튼**을 누르면 드론 영상이 작은 창으로 뜹니다 (‘크게’로 키우기).

## 4-1. 노트북 브리지 방식 (드론 ─ 텔레메트리 ─ 노트북 ─ Wi-Fi ─ 태블릿)

앱이 파이 대신 **노트북 브리지**(`bridge/laptop_bridge.bat`)에 연결하는 방식입니다. 자세한 순서는 맨 위 `README.md`의 4-01.
- 파이 `config.env`: `QGC_OUT=""` (QGC는 노트북에서 무선기로), `TOKEN`은 노트북 배치 파일과 같게.
- 파이의 `drone-bridge`는 필요 없음: `sudo systemctl disable --now drone-bridge`. `drone-mavproxy`(카메라가 위치를 받음)와 `drone-camera`는 그대로.
- 노트북 브리지가 경로를 올릴 때 이 파이의 `http://<파이>:8080/mission`으로 임무 정보를 보내고, 사진·탐지는 `/photos`·`/detections`·`/files/...`로 가져갑니다.
- PX4: 무선기 포트 TELEM1은 기본값, 두 포트 모두 `MAV_0_FORWARD = 1`, `MAV_1_FORWARD = 1` (파이의 탐지 알림이 무선기로 넘어감).

## 5. 임무 중에 하는 일

1. 앱에서 경로를 올리면 브리지가 `~/drone-data/local/mission.json`(임무 번호·칸 격자·촬영 간격)을 씁니다.
2. 드론이 **시동 + 미션 모드 + 수색 구역 안 + 비행 고도의 60% 이상**일 때, 촬영 간격(m)마다 사진을 찍습니다.
   - `out/<임무>/<기체>/full/` 원본, `thumb/` 앱용 축소본(가로 640), `thermal/` 열화상
   - 사진 정보는 `photos.jsonl` → 브리지가 바로 앱에 보냄 → 지도에서 칸을 누르면 사진이 보임
3. 열화상(MLX90640)이 있으면 사진마다 평균 온도를 같이 올려 앱 '온도 보기' 열지도가 채워지고,
   주변보다 3°C 이상 따뜻한 점(사람 후보)과 45°C 이상(불)은 **탐지**로 앱에 알림이 갑니다.
   - 같은 알림을 MAVLink(`DET d1 위도 경도 온도`)로도 보내서, 텔레메트리 무선기로 연결해도 알림을 받습니다 (PX4 `MAV_0_FORWARD = 1`, `MAV_1_FORWARD = 1` 필요).
4. 사진 시각은 PX4의 GPS 시각으로 맞춥니다 (파이는 인터넷이 없으면 시계가 틀림).

## 6. 열화상 (선택)

MLX90640 연결: 3V3 → 1번 핀, GND → 6번 핀, SDA → 3번 핀(GPIO2), SCL → 5번 핀(GPIO3).
연결돼 있지 않으면 일반 사진만 찍고 계속 동작합니다 (로그에 `[열화상] 없음`).
- 뒷면 표기 BAA = 55°×35° (기본값), BAB = 110°×75° → `config.env`의 `HFOV`·`VFOV`를 바꾸세요.
- 화소가 32×24뿐이라 **비행 고도 50m 이하**(30~40m 권장)에서 사람을 찾을 수 있습니다. 앱 드론 관리에서 비행 고도를 맞추세요.
- 사람은 화소보다 작아 화소 온도가 낮게 나오므로, 화소 안 사람 비율로 되돌린 **추정 온도**(34.5~42°C)를 앱에 보냅니다. 실제 화소 온도는 `rawC`, 주변 온도는 `bgC`로 함께 기록됩니다.

## 7. 파이 없이 시험 (PC)

```bash
python3 bridge/gcs_bridge.py --demo --drones 1 --local ./local --token pw
python3 pi/pi_camera.py --local ./local --fake --fake-hot 33.4512,126.5715
```
앱: 브리지 주소 `ws://<PC IP>:8766`, 드론 영상 주소 `http://<PC IP>:8080/video`.
경로를 올리면 가짜 드론이 그 경로를 날며 가짜 사진을 찍고, `--fake-hot` 위치를 지나면 탐지가 앱에 뜹니다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| 앱에 드론이 안 보임 | `journalctl -u drone-bridge -f` 에 하트비트가 오는지. `config.env`의 `SERIAL`·`BAUD`, PX4 `SER_TEL2_BAUD = 921600`, `MAV_1_CONFIG = TELEM 2` |
| 영상이 안 나옴 | `cam_test.py`를 끄고 `journalctl -u drone-camera -f`. CSI 카메라면 로그에 `[카메라] 열림`이 나와야 함 |
| 사진이 안 찍힘 | 앱에서 경로를 올렸는지(`mission.json`), 드론이 미션 모드인지, 고도가 비행 고도의 60% 이상인지 |
| QGC가 안 붙음 | `config.env`의 `QGC_OUT`에 노트북 IP가 맞는지 (노트북 IP가 바뀌면 고치고 `sudo systemctl restart drone-mavproxy`) |
