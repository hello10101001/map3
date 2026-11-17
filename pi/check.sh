#!/bin/bash
# 드론 라즈베리파이 점검: 서비스 · 시리얼 · 픽스호크 신호 · 카메라 · 열화상 · 저장 공간
#   bash ~/drone-gcs/pi/check.sh
DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -a; . "$DIR/pi/config.env"; set +a
ok(){ echo "  ✅ $*"; }; bad(){ echo "  ❌ $*"; }

echo "[1] 서비스"
SVCS="drone-mavproxy drone-camera"; [ "$MODE" = "wifi" ] && SVCS="$SVCS drone-bridge"
for s in $SVCS; do systemctl is-active --quiet "$s" && ok "$s 실행 중" || bad "$s 꺼짐 → journalctl -u $s -n 30"; done

echo "[2] 시리얼 ($SERIAL)"
[ -e "$SERIAL" ] && ok "$SERIAL 있음" || bad "$SERIAL 없음 → sudo raspi-config 에서 시리얼 하드웨어 켜고 재부팅"
id -nG | grep -qw dialout && ok "dialout 그룹" || bad "dialout 그룹 아님 → 재부팅 (install.sh 가 넣었음)"
pgrep -af "cam_test.py" >/dev/null && bad "cam_test.py 가 켜져 있음 → 끄세요 (8080 포트 충돌)"

echo "[3] 픽스호크 신호 (mavproxy 출구 14561을 잠깐 빌려 확인 — 카메라 서비스를 3초 멈춤)"
sudo systemctl stop drone-camera
python3 - <<'P'
from pymavlink import mavutil
c = mavutil.mavlink_connection("udpin:127.0.0.1:14561")
m = c.recv_match(type="HEARTBEAT", blocking=True, timeout=3)
while m is not None and m.autopilot == 8:
    m = c.recv_match(type="HEARTBEAT", blocking=True, timeout=3)
if m is None:
    print("  ❌ 하트비트 없음 → 배선(TX↔RX), PX4 SER_TEL2_BAUD=921600, MAV_1_CONFIG=TELEM 2, config.env BAUD 확인")
else:
    print(f"  ✅ 기체 {m.get_srcSystem()}번 하트비트 (autopilot={m.autopilot}, 12=PX4)")
    p = c.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=3)
    print(f"  ✅ 위치 {p.lat/1e7:.6f}, {p.lon/1e7:.6f}" if p else "  ⚠ 위치 아직 없음 (GPS 잡히기 전이면 정상)")
P
sudo systemctl start drone-camera

echo "[4] 카메라 · 영상"
sleep 3
code=$(curl -s -o /tmp/snap.jpg -w "%{http_code}" "http://127.0.0.1:${PORT:-8080}/snapshot")
[ "$code" = "200" ] && ok "카메라 화면 받음 → 브라우저에서 http://$(hostname -I | awk '{print $1}'):8080/video" || bad "카메라 화면 없음 → journalctl -u drone-camera -n 30"

echo "[5] 열화상"
journalctl -u drone-camera -b --no-pager | grep -q "MLX90640 연결됨" && ok "MLX90640 연결됨" || echo "  ⚠ 열화상 없음 (없어도 일반 사진은 찍음) → i2cdetect -y 1 에 33이 보여야 함"

echo "[6] 저장 공간"
df -h "$LOCAL" | awk 'NR==2{print "  남은 공간 "$4" (사진 폴더 '"$LOCAL"')"}'

echo "[7] 이 파이 주소"
echo "  $(hostname -I)"
