#!/bin/bash
# 드론 라즈베리파이 설치: 패키지 + 자동 실행 서비스 3개 (mavproxy 중계 · 앱 브리지 · 카메라)
#   cd ~/drone-gcs/pi && bash install.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"     # drone-gcs 폴더
USER_NAME="$(id -un)"
PY=/usr/bin/python3

echo "== 패키지 설치"
sudo apt update
sudo apt install -y python3-opencv python3-numpy python3-pip
pip install mavproxy pymavlink pyserial --break-system-packages
# 열화상 MLX90640을 쓸 때만 (없어도 됨)
pip install adafruit-circuitpython-mlx90640 adafruit-blinka --break-system-packages || echo "  (열화상 라이브러리 설치 실패 — 열화상 없이 동작)"

echo "== 시리얼(픽스호크) · I2C(열화상) 켜기"
sudo raspi-config nonint do_serial_hw 0      # 시리얼 하드웨어 켬
sudo raspi-config nonint do_serial_cons 1    # 시리얼 로그인 셸 끔 (픽스호크와 충돌)
sudo raspi-config nonint do_i2c 0 || true
grep -q "i2c_arm_baudrate" /boot/firmware/config.txt 2>/dev/null || echo "dtparam=i2c_arm_baudrate=1000000" | sudo tee -a /boot/firmware/config.txt >/dev/null
for g in dialout i2c video gpio; do   # 없는 그룹이 하나 있어도 나머지는 넣음
  getent group "$g" >/dev/null && sudo usermod -aG "$g" "$USER_NAME"
done

# 설정 파일의 사진 폴더를 지금 사용자로
sed -i "s#^LOCAL=.*#LOCAL=/home/$USER_NAME/drone-data/local#" "$DIR/pi/config.env"
mkdir -p "/home/$USER_NAME/drone-data/local/out"

CAMWRAP=""
command -v libcamerify >/dev/null && CAMWRAP="$(command -v libcamerify) "   # CSI 카메라를 OpenCV로 (cam_test.py처럼)

echo "== 서비스 등록"
sudo tee /etc/systemd/system/drone-mavproxy.service >/dev/null <<S
[Unit]
Description=Drone MAVLink router (pixhawk serial -> bridge, camera, QGC)
After=network-online.target
[Service]
User=$USER_NAME
EnvironmentFile=$DIR/pi/config.env
ExecStart=/bin/bash -c 'OUTS="--out=udp:127.0.0.1:14560 --out=udp:127.0.0.1:14561"; for o in \$QGC_OUT; do OUTS="\$OUTS --out=\$o"; done; exec $PY -m MAVProxy.mavproxy --master=\$SERIAL --baudrate \$BAUD \$OUTS --daemon --non-interactive --state-basedir=/tmp'
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
S

sudo tee /etc/systemd/system/drone-bridge.service >/dev/null <<S
[Unit]
Description=Drone GCS bridge (app <-> MAVLink, Wi-Fi photos)
After=drone-mavproxy.service
[Service]
User=$USER_NAME
EnvironmentFile=$DIR/pi/config.env
ExecStart=/bin/bash -c 'exec $PY -u $DIR/bridge/gcs_bridge.py --mav udpin:127.0.0.1:14560 --local \$LOCAL --token \$TOKEN'
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
S

sudo tee /etc/systemd/system/drone-camera.service >/dev/null <<S
[Unit]
Description=Drone camera (live video, mission photos, thermal detection)
After=drone-mavproxy.service
[Service]
User=$USER_NAME
EnvironmentFile=$DIR/pi/config.env
ExecStart=/bin/bash -c 'exec ${CAMWRAP}$PY -u $DIR/pi/pi_camera.py --local \$LOCAL --camera \$CAMERA --width \$WIDTH --height \$HEIGHT --hfov \$HFOV --vfov \$VFOV --token \$TOKEN'
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
S

sudo systemctl daemon-reload
MODE="$(grep -E '^MODE=' "$DIR/pi/config.env" | cut -d= -f2 | tr -d '\" ')"
if [ "$MODE" = "wifi" ]; then
  sudo systemctl enable drone-mavproxy drone-bridge drone-camera
  echo "연결 방식: wifi — 태블릿이 파이에 바로 연결 (ws://<파이 IP>:8766)"
else
  sudo systemctl enable drone-mavproxy drone-camera
  sudo systemctl disable --now drone-bridge 2>/dev/null || true
  echo "연결 방식: laptop — 노트북 laptop_bridge.bat 이 앱과 통신, 파이는 카메라만"
fi
chmod +x "$DIR/pi/check.sh" 2>/dev/null || true
echo
echo "설치 끝. 재부팅하면 자동으로 켜집니다:  sudo reboot"
echo "재부팅 뒤 점검:  bash ~/drone-gcs/pi/check.sh"
