#!/usr/bin/env python3
"""
드론 라즈베리파이 촬영 프로그램 — 실시간 영상 + 임무 사진 + 열화상 탐지

  하는 일
  1) 실시간 영상: http://<파이 IP>:8080/video (MJPEG). 앱 지도 화면의 '영상' 버튼으로 봄. (cam_test.py와 같은 방식)
  2) 임무 사진: 앱이 경로를 올리면 브리지(gcs_bridge.py --local)가 mission.json을 씀 → 이 프로그램이 보고,
     드론이 시동 + 미션 모드 + 수색 구역 안에 있을 때 촬영 간격(m)마다 사진을 찍어
     out/<임무>/photos.jsonl 에 기록 → 브리지가 앱에 보냄 (앱에서 칸을 누르면 사진이 보임)
  3) 열화상 (MLX90640이 연결돼 있으면): 사진마다 평균 온도(칸 열지도), 주변보다 뜨거운 점은 탐지로
     out/<임무>/detections.jsonl 에 기록 + MAVLink 알림 "DET <번호> <위도> <경도> <온도>" (텔레메트리 무선기로도 앱에 감)

  연결 (파이 안)
    픽스호크 ─ /dev/serial0 ─ mavproxy ─┬─ udp 노트북(QGroundControl)
                                       ├─ udp 127.0.0.1:14560 → gcs_bridge.py (앱과 통신)
                                       └─ udp 127.0.0.1:14561 → 이 프로그램 (위치·모드)

  노트북 브리지 방식 (드론 ─ 텔레메트리 ─ 노트북 브리지 ─ 앱)
    노트북 브리지를 --pi http://<파이 IP>:8080 으로 켜면, 경로를 올릴 때 브리지가 이 프로그램에 임무 정보를 보내고
    (POST /mission), 파이 Wi-Fi에 닿을 때 사진·탐지 목록(/photos, /detections)과 사진 파일(/files/...)을 가져갑니다.

  실행
    libcamerify python3 pi_camera.py --local ~/drone-data/local          # CSI 카메라 (cam_test.py처럼 libcamerify)
    python3 pi_camera.py --local ~/drone-data/local --camera 0            # USB 카메라
    python3 pi_camera.py --local ./local --fake                           # 파이·카메라 없이 시험 (가짜 드론이 mission.json 경로를 날며 찍음)
"""
import argparse, hmac, json, math, os, shutil, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import cv2
import numpy as np

M_LAT = 111320.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ======================= 카메라 =======================
class Camera:
    """마지막 화면을 계속 갖고 있음 (영상 스트림과 사진이 같이 씀)"""

    def __init__(self, dev, w, h, fake=False):
        self.dev, self.w, self.h, self.fake = dev, w, h, fake
        self.frame, self.at, self.lock = None, 0.0, threading.Lock()
        self.jpg, self.jpg_at = None, 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _open(self):
        cap = cv2.VideoCapture(self.dev)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.h)
        return cap

    def _run(self):
        cap, fails = None, 0
        while True:
            if self.fake:
                f = np.full((self.h, self.w, 3), (60, 110, 70), np.uint8)
                cv2.putText(f, time.strftime("%H:%M:%S"), (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
                self._set(f)
                time.sleep(0.1)
                continue
            if cap is None or not cap.isOpened():
                cap = self._open()
                if not cap.isOpened():
                    log("[카메라] 열 수 없음 — 5초 뒤 다시 (CSI 카메라면 libcamerify로 실행했는지 확인)")
                    time.sleep(5)
                    continue
                log(f"[카메라] 열림 {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}×{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            ok, f = cap.read()
            if not ok:
                fails += 1
                if fails > 20:   # 계속 실패하면 다시 열기 (케이블 흔들림 등)
                    cap.release()
                    cap, fails = None, 0
                time.sleep(0.05)
                continue
            fails = 0
            self._set(f)

    def _set(self, f):
        with self.lock:
            self.frame, self.at = f, time.time()

    def latest(self, max_age=1.0):
        with self.lock:
            if self.frame is None or time.time() - self.at > max_age:
                return None
            return self.frame.copy()

    def stream_jpeg(self):
        """영상용 JPEG (가로 640으로 줄여서, 같은 화면은 한 번만 압축 → 여러 명이 봐도 가벼움)"""
        with self.lock:
            f, at = self.frame, self.at
        if f is None:
            return None
        if at != self.jpg_at:
            if f.shape[1] > 640:
                f = cv2.resize(f, (640, int(f.shape[0] * 640 / f.shape[1])))
            ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                self.jpg, self.jpg_at = buf.tobytes(), at
        return self.jpg


# ======================= 열화상 (MLX90640, 있으면) =======================
class Thermal:
    W, H = 32, 24

    def __init__(self, fake=False, rate=4):
        self.fake, self.arr, self.at, self.lock, self.ok = fake, None, 0.0, threading.Lock(), False
        self.hot = None   # 가짜 모드: 뜨거운 점 위치(위도, 경도) — 드론이 근처를 지나면 화면에 나타남
        if fake:
            self.ok = True
            return
        try:
            import board, busio, adafruit_mlx90640   # pip install adafruit-circuitpython-mlx90640
            i2c = busio.I2C(board.SCL, board.SDA, frequency=800000)
            self.mlx = adafruit_mlx90640.MLX90640(i2c)
            self.mlx.refresh_rate = {2: adafruit_mlx90640.RefreshRate.REFRESH_2_HZ, 4: adafruit_mlx90640.RefreshRate.REFRESH_4_HZ,
                                     8: adafruit_mlx90640.RefreshRate.REFRESH_8_HZ}.get(rate, adafruit_mlx90640.RefreshRate.REFRESH_4_HZ)
            self.ok = True
            threading.Thread(target=self._run, daemon=True).start()
            log("[열화상] MLX90640 연결됨")
        except Exception as e:
            log(f"[열화상] 없음 — 일반 사진만 찍습니다 ({e.__class__.__name__}: {e})")

    def _run(self):
        buf = [0.0] * 768
        while True:
            try:
                self.mlx.getFrame(buf)
                a = np.array(buf, np.float32).reshape(self.H, self.W)
                if np.all((a > -40) & (a < 300)):   # 깨진 값이 있는 화면은 버림
                    with self.lock:
                        self.arr, self.at = a, time.time()
            except (ValueError, RuntimeError, OSError):
                time.sleep(0.05)   # I2C 읽기 오류는 가끔 생김 → 다음 화면

    def latest(self, max_age=1.5):
        with self.lock:
            if self.arr is None or time.time() - self.at > max_age:
                return None
            return self.arr.copy()

    def fake_frame(self, v, hfov, vfov):
        """가짜 열화상: 15°C 땅 + 가짜 사람(36°C)이 화각 안에 있으면 그 화소가 따뜻함"""
        a = np.random.normal(15, 0.3, (self.H, self.W)).astype(np.float32)
        if self.hot and v.alt and v.alt > 2:
            k = M_LAT * math.cos(math.radians(v.lat))
            n, e = (self.hot[0] - v.lat) * M_LAT, (self.hot[1] - v.lng) * k
            h = math.radians(v.hdg or 0)
            fwd, right = n * math.cos(h) + e * math.sin(h), -n * math.sin(h) + e * math.cos(h)
            x = (math.degrees(math.atan2(right, v.alt)) / hfov + 0.5) * self.W
            y = (0.5 - math.degrees(math.atan2(fwd, v.alt)) / vfov) * self.H
            if 0 <= x < self.W and 0 <= y < self.H:
                a[int(y), int(x)] = 26.0   # 화소 안에 사람이 일부만 → 주변보다 +11°C
        return a


def colorize(a, size=(640, 480)):
    """열화상을 보기 좋게: 부드럽게 키우고 색 입힘 (색 범위 최소 5°C)"""
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 5:
        hi = lo + 5
    g = np.clip((a - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    return cv2.applyColorMap(cv2.resize(g, size, interpolation=cv2.INTER_CUBIC), cv2.COLORMAP_INFERNO)


def find_hot(a, delta, fire):
    """주변(5×5 중앙값)보다 delta°C 이상 따뜻하거나 fire°C 이상인 덩어리 → [(x, y, 최고온도, 주변온도)]"""
    bg = cv2.medianBlur(a.astype(np.float32), 5)
    mask = (((a - bg) >= delta) | (a >= fire)).astype(np.uint8)
    n, lab = cv2.connectedComponents(mask)
    out = []
    for i in range(1, n):
        ys, xs = np.where(lab == i)
        j = int(np.argmax(a[ys, xs]))
        y, x = int(ys[j]), int(xs[j])
        out.append((x, y, float(a[y, x]), float(bg[y, x])))
    return out


# ======================= 드론 상태 (MAVLink) =======================
class VehicleState:
    """촬영에 쓰는 드론 상태 (실기체·가짜 공통)"""

    def __init__(self, sysid=None):
        self.lat = self.lng = self.alt = self.hdg = None
        self.armed, self.mission_mode, self.sysid, self.pos_at = False, False, sysid, 0.0
        self.time_off = 0.0   # PX4 GPS 시각 - 파이 시각 (파이 시계는 인터넷 없으면 틀림)

    def now_ms(self):
        return int((time.time() + self.time_off) * 1000)

    def fresh(self):
        return self.lat is not None and time.time() - self.pos_at < 2


class Vehicle(VehicleState):
    """mavproxy 출구(udp)에서 위치·시동·모드를 받음. 탐지 알림(STATUSTEXT)을 보냄"""

    def __init__(self, url):
        super().__init__()
        from pymavlink import mavutil   # mavproxy를 설치하면 같이 깔림
        self.mavutil = mavutil
        self.conn = mavutil.mavlink_connection(url, source_system=1, source_component=191)   # 191 = 보조 컴퓨터
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        last_hb = 0
        while True:
            if time.time() - last_hb > 1:
                with self.lock:   # 촬영 스레드의 탐지 알림과 동시에 보내지 않게
                    try:
                        self.conn.mav.heartbeat_send(18, 8, 0, 0, 4)   # 보조 컴퓨터 (비행 제어기 아님 → 앱·브리지가 무시)
                    except Exception:
                        pass
                last_hb = time.time()
            try:
                m = self.conn.recv_match(blocking=True, timeout=0.5)
            except Exception as e:   # 깨진 데이터 등으로 받기 스레드가 죽지 않게
                log("[MAVLink] 받기 오류", e)
                time.sleep(0.5)
                continue
            if m is None:
                continue
            t = m.get_type()
            if t == "HEARTBEAT":
                if m.autopilot == 8 or m.type == 6:   # 지상국·카메라 등
                    continue
                self.sysid = m.get_srcSystem()
                self.conn.mav.srcSystem = self.sysid   # 기체와 같은 번호로 알림을 보냄
                self.armed = bool(m.base_mode & 128)
                main, sub = (m.custom_mode >> 16) & 0xff, (m.custom_mode >> 24) & 0xff
                self.mission_mode = main == 4 and sub == 4   # PX4 AUTO.MISSION
            elif t == "GLOBAL_POSITION_INT":
                self.lat, self.lng = m.lat / 1e7, m.lon / 1e7
                self.alt, self.pos_at = m.relative_alt / 1000.0, time.time()
                if m.hdg != 65535:
                    self.hdg = m.hdg / 100.0
            elif t == "SYSTEM_TIME" and m.time_unix_usec > 1.6e15:
                self.time_off = m.time_unix_usec / 1e6 - time.time()

    def notify(self, text):
        with self.lock:
            try:
                self.conn.mav.statustext_send(6, text.encode()[:50])   # 6 = 안내
            except Exception as e:
                log("[MAVLink] 알림 보내기 실패", e)


class FakeVehicle(VehicleState):
    """시험용: mission.json 경로를 따라 날아가는 가짜 드론"""

    def __init__(self, speed=15):
        super().__init__(sysid=1)
        self.speed, self.sent, self.route = speed, [], None

    def fly(self, route, home):
        self.route = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 50.0) for p in route] + [(home[0], home[1], 0.0)]
        self.lat, self.lng, self.alt = home[0], home[1], 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.armed = self.mission_mode = True
        for la, lo, z in self.route:
            while True:
                k = M_LAT * math.cos(math.radians(self.lat))
                dn, de = (la - self.lat) * M_LAT, (lo - self.lng) * k
                d = math.hypot(dn, de)
                if d > 0.5:
                    self.hdg = math.degrees(math.atan2(de, dn)) % 360
                step = self.speed * 0.1
                if d <= step:
                    self.lat, self.lng, self.alt = la, lo, z if z > 0 else self.alt
                    break
                self.lat += dn / d * step / M_LAT
                self.lng += de / d * step / k
                self.alt += (z - self.alt) * 0.2
                self.pos_at = time.time()
                time.sleep(0.1)
        self.alt, self.armed, self.mission_mode, self.pos_at = 0.0, False, False, time.time()
        log("[가짜 드론] 착륙")

    def notify(self, text):
        self.sent.append(text)
        log("[가짜 MAVLink]", text)


# ======================= 임무 정보 (브리지가 쓴 mission.json) =======================
def cell_of(g, lat, lng):
    x = (lng - g["lng0"]) * M_LAT * math.cos(math.radians(g["lat0"]))
    y = (lat - g["lat0"]) * M_LAT
    c, r = math.floor((x - g["x0"]) / g["cw"]), math.floor((y - g["y0"]) / g["ch"])
    return r * g["cols"] + c if 0 <= c < g["cols"] and 0 <= r < g["rows"] else None


class Mission:
    def __init__(self, root):
        self.path, self.mtime, self.doc = os.path.join(root, "mission.json"), None, None

    def poll(self):
        """새 임무면 True"""
        try:
            t = os.path.getmtime(self.path)
        except OSError:
            return False
        if t == self.mtime:
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return False   # 브리지가 쓰는 중 → 다음에
        self.mtime, self.doc = t, doc
        return True

    def drone(self, sysid):
        ds = (self.doc or {}).get("drones") or {}
        return ds.get(str(sysid)) or (next(iter(ds.values())) if ds else None)


# ======================= 촬영 =======================
class Shooter:
    def __init__(self, a, cam, th, veh, mis):
        self.a, self.cam, self.th, self.veh, self.mis = a, cam, th, veh, mis
        self.out = os.path.join(a.local, "out")
        self.last = None   # 마지막 촬영 위치
        self.n = 0
        self.dets = []     # 최근 탐지 (중복 합치기)
        self.det_n = 0

    def loop(self):
        while True:
            if self.mis.poll():
                d = self.mis.drone(self.veh.sysid)
                log(f"[임무] {self.mis.doc.get('mission')} · 촬영 간격 {d and d.get('shot')}m · 칸 {self.mis.doc['grid']['cols']}×{self.mis.doc['grid']['rows']}")
                self.last, self.dets = None, []
                if self.a.fake and d:
                    if self.th.hot is None:   # 가짜 사람: 경로 가운데 근처
                        r = d["route"][len(d["route"]) // 2]
                        self.th.hot = (float(r[0]) + 0.00005, float(r[1]))
                    self.veh.fly(d["route"], d["home"])
            self.tick()
            time.sleep(0.1)

    def tick(self):
        doc, v = self.mis.doc, self.veh
        if not doc or not doc.get("grid") or not v.armed or not v.mission_mode or not v.fresh():
            return
        d = self.mis.drone(v.sysid) or {}
        shot = float(d.get("shot") or self.a.shot)
        if (v.alt or 0) < float(d.get("alt") or 30) * 0.6:   # 이륙·착륙 중에는 안 찍음
            return
        cell = cell_of(doc["grid"], v.lat, v.lng)
        if cell is None:   # 수색 구역 밖
            return
        if self.last:
            k = M_LAT * math.cos(math.radians(v.lat))
            if math.hypot((v.lat - self.last[0]) * M_LAT, (v.lng - self.last[1]) * k) < shot:
                return
        self.last = (v.lat, v.lng)
        try:
            self.capture(doc["mission"], cell)
        except Exception as e:
            log("[촬영] 오류", e)

    def capture(self, mid, cell):
        v, a = self.veh, self.a
        lat, lng, alt, hdg, at = v.lat, v.lng, v.alt, v.hdg or 0.0, v.now_ms()
        frame = self.cam.latest()
        therm = self.th.fake_frame(v, a.hfov, a.vfov) if (self.th.ok and a.fake) else (self.th.latest() if self.th.ok else None)
        if frame is None and therm is None:
            log("[촬영] 카메라 화면 없음 — 건너뜀")
            return
        sid = str(v.sysid or 1)
        base = os.path.join(self.out, mid, sid)
        for sub in ("full", "thumb", "thermal"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        self.n += 1
        # 이름 = GPS 시각(ms) + 순번: 파이를 다시 켜서 시계가 되돌아가도 같은 이름으로 덮어쓰지 않게
        name = f"{at}_{self.n:04d}"
        low = shutil.disk_usage(self.out).free < self.a.min_free_mb * 1024 * 1024   # SD카드가 거의 차면 원본은 빼고 축소본만
        rel = lambda sub, ext="jpg": f"{mid}/{sid}/{sub}/{name}.{ext}"
        p = {"id": f"{sid}-{name}", "drone": int(sid), "lat": round(lat, 7), "lng": round(lng, 7), "alt": round(alt, 1),
             "hdg": round(hdg, 1), "takenAt": at, "cell": cell}
        if frame is not None:
            th = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1]))) if frame.shape[1] > 640 else frame
            cv2.imwrite(os.path.join(self.out, rel("thumb")), th, [cv2.IMWRITE_JPEG_QUALITY, 75])
            p["thumb"] = p["file"] = rel("thumb")
            if not low:
                cv2.imwrite(os.path.join(self.out, rel("full")), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                p["file"] = rel("full")
            elif self.n % 50 == 1:
                log(f"[촬영] SD카드 남은 공간이 {self.a.min_free_mb}MB보다 적어 원본은 저장하지 않습니다")
        if therm is not None:
            p["tempAvgC"] = round(float(therm.mean()), 2)
            cv2.imwrite(os.path.join(self.out, rel("thermal")), colorize(therm), [cv2.IMWRITE_JPEG_QUALITY, 80])
            p["thermal"] = rel("thermal")
            if "thumb" not in p:
                p["thumb"] = p["file"] = rel("thermal")
            self.detect(mid, sid, name, therm, lat, lng, alt, hdg, at, cell)
        self.append(mid, "photos", p)

    def detect(self, mid, sid, name, a, lat, lng, alt, hdg, at, cell):
        A = self.a
        for x, y, t, bg in find_hot(a, A.delta, A.fire):
            # 화면 위치 → 땅 위치 (짐벌이 바로 아래를 본다고 가정, 사진 위쪽 = 기체 앞쪽)
            ax = math.radians(((x + 0.5) / Thermal.W - 0.5) * A.hfov)
            ay = math.radians((0.5 - (y + 0.5) / Thermal.H) * A.vfov)
            right, fwd, h = alt * math.tan(ax), alt * math.tan(ay), math.radians(hdg)
            n, e = fwd * math.cos(h) - right * math.sin(h), fwd * math.sin(h) + right * math.cos(h)
            la, lo = lat + n / M_LAT, lng + e / (M_LAT * math.cos(math.radians(lat)))
            k = M_LAT * math.cos(math.radians(la))
            if any(math.hypot((la - d[0]) * M_LAT, (lo - d[1]) * k) < A.dedup_m and at - d[2] < A.dedup_s * 1000 for d in self.dets):
                continue   # 같은 열원을 여러 장에서 봄 → 하나로
            self.dets.append((la, lo, at))
            if t >= A.fire:
                temp = t
            else:
                # 사람은 화소보다 작아 화소 온도가 낮게 나옴 → 화소 안 사람 비율(약 0.5㎡)로 되돌린 추정 온도
                pw = 2 * alt * math.tan(math.radians(A.hfov / 2)) / Thermal.W
                ph = 2 * alt * math.tan(math.radians(A.vfov / 2)) / Thermal.H
                frac = min(1.0, 0.5 / max(pw * ph, 0.01))
                temp = min(42.0, max(34.5, bg + (t - bg) / frac))
            self.det_n += 1
            did = f"d{int(time.time()) % 100000}{self.det_n}"   # 프로그램을 다시 켜도 같은 임무 안에서 번호가 겹치지 않게 (앱이 같은 번호는 하나로 봄)
            img = colorize(a)
            sx, sy = img.shape[1] / Thermal.W, img.shape[0] / Thermal.H
            cv2.circle(img, (int((x + 0.5) * sx), int((y + 0.5) * sy)), 22, (255, 255, 255), 2)
            cv2.putText(img, f"{t:.1f}C", (int((x + 0.5) * sx) + 26, int((y + 0.5) * sy) + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            rel = f"{mid}/{sid}/thermal/{name}_{did}.jpg"
            cv2.imwrite(os.path.join(self.out, rel), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            self.append(mid, "detections", {"id": did, "drone": int(sid), "lat": round(la, 7), "lng": round(lo, 7), "alt": round(alt, 1),
                                            "tempC": round(temp, 1), "rawC": round(t, 1), "bgC": round(bg, 1), "takenAt": at, "cell": cell, "img": rel})
            self.veh.notify(f"DET {did} {la:.7f} {lo:.7f} {temp:.1f}")   # 텔레메트리 무선기로도 앱에 알림
            log(f"[탐지] {did} {temp:.1f}°C (화소 {t:.1f}°C, 주변 {bg:.1f}°C) {la:.6f},{lo:.6f}")

    def append(self, mid, kind, obj):
        with open(os.path.join(self.out, mid, kind + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")   # 한 줄을 끝까지 쓴 뒤 브리지가 읽음


# ======================= 영상 서버 =======================
def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass   # 쓰는 중인 마지막 줄
    except OSError:
        pass
    return out


def serve(cam, port, local, token):
    out_root = os.path.realpath(os.path.join(local, "out"))
    CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _q(self):
            u = urlsplit(self.path)
            return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

        def _authed(self, q):
            return not token or hmac.compare_digest(q.get("t", "").encode(), token.encode())

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # 노트북 브리지가 경로를 올릴 때 임무 정보(mission.json)를 보냄
            path, q = self._q()
            if path != "/mission":
                return self._send(404, b'{"error":"not found"}')
            if not self._authed(q):
                return self._send(403, b'{"error":"token"}')
            try:
                n = int(self.headers.get("Content-Length", "0"))
                doc = json.loads(self.rfile.read(min(n, 2_000_000)))
                assert doc.get("mission") and doc.get("grid")
            except Exception:
                return self._send(400, b'{"error":"bad mission"}')
            tmp = os.path.join(local, "mission.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(local, "mission.json"))
            log(f"[임무] 노트북 브리지에서 받음: {doc['mission']}")
            self._send(200, b'{"ok":true}')

        def do_GET(self):
            path, q = self._q()
            if (path in ("/photos", "/detections", "/mission") or path.startswith("/files/")) and not self._authed(q):
                return self._send(403, b'{"error":"token"}')
            if path == "/mission":   # 지금 임무 정보 (노트북 브리지를 다시 켰을 때 이어 받기용)
                try:
                    with open(os.path.join(local, "mission.json"), "rb") as f:
                        return self._send(200, f.read(), extra={"Cache-Control": "no-store"})
                except OSError:
                    return self._send(200, b"{}", extra={"Cache-Control": "no-store"})
            if path in ("/photos", "/detections"):   # 사진·탐지 목록 (out/<임무>/*.jsonl 그대로)
                mid = os.path.basename(q.get("mission", ""))
                lst = read_jsonl(os.path.join(out_root, mid, path[1:] + ".jsonl")) if mid else []
                return self._send(200, json.dumps(lst, ensure_ascii=False).encode("utf-8"), extra={"Cache-Control": "no-store"})
            if path.startswith("/files/"):
                p = os.path.realpath(os.path.join(out_root, unquote(path[len("/files/"):])))
                if not p.startswith(out_root + os.sep) or not os.path.isfile(p):   # 폴더 밖(../) 막음
                    return self._send(404, b'{"error":"not found"}')
                with open(p, "rb") as f:
                    return self._send(200, f.read(), CT.get(os.path.splitext(p)[1].lower(), "application/octet-stream"), {"Cache-Control": "max-age=86400"})
            if self.path.startswith("/video"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    while True:
                        j = cam.stream_jpeg()
                        if j:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
                        time.sleep(0.1)   # 초당 약 10장 (Wi-Fi 부담 줄임)
                except OSError:   # 앱이 영상 창을 닫음 (BrokenPipe·ConnectionReset 등)
                    return
            elif self.path.startswith("/snapshot"):
                j = cam.stream_jpeg()
                self.send_response(200 if j else 503)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if j:
                    self.wfile.write(j)
            else:
                b = "<!doctype html><meta charset=utf-8><title>드론 영상</title><body style='margin:0;background:#000'><img src='/video' style='width:100%'>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"[영상] http://<파이 IP>:{port}/video")


def main():
    ap = argparse.ArgumentParser(description="드론 라즈베리파이 촬영 프로그램")
    ap.add_argument("--local", default=os.path.expanduser("~/drone-data/local"), help="브리지 --local 과 같은 폴더")
    ap.add_argument("--mav", default="udpin:127.0.0.1:14561", help="mavproxy 출구 (--out=udp:127.0.0.1:14561)")
    ap.add_argument("--camera", default="0", help="카메라 번호 또는 장치 경로")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=960)
    ap.add_argument("--port", type=int, default=8080, help="실시간 영상 포트")
    ap.add_argument("--shot", type=float, default=30, help="임무에 촬영 간격이 없을 때 (m)")
    ap.add_argument("--hfov", type=float, default=55, help="열화상 가로 화각 (MLX90640 BAA 55°, BAB 110°)")
    ap.add_argument("--vfov", type=float, default=35, help="열화상 세로 화각 (BAA 35°, BAB 75°)")
    ap.add_argument("--delta", type=float, default=3.0, help="주변보다 이만큼(°C) 따뜻하면 사람 후보")
    ap.add_argument("--fire", type=float, default=45.0, help="이 온도(°C) 이상은 불")
    ap.add_argument("--dedup-m", type=float, default=10)
    ap.add_argument("--dedup-s", type=float, default=30)
    ap.add_argument("--min-free-mb", type=float, default=500, help="SD카드 남은 공간이 이보다 적으면 원본 사진은 저장하지 않음")
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""), help="사진·임무 주고받을 때 비밀번호 (브리지 --token과 같게)")
    ap.add_argument("--fake", action="store_true", help="파이·카메라 없이 시험")
    ap.add_argument("--fake-hot", help="가짜 사람 위치 '위도,경도' (--fake)")
    a = ap.parse_args()
    os.makedirs(os.path.join(a.local, "out"), exist_ok=True)
    dev = int(a.camera) if a.camera.isdigit() else a.camera
    cam = Camera(dev, a.width, a.height, fake=a.fake)
    th = Thermal(fake=a.fake)
    if a.fake and a.fake_hot:
        th.hot = tuple(map(float, a.fake_hot.split(",")))
    veh = FakeVehicle() if a.fake else Vehicle(a.mav)
    serve(cam, a.port, a.local, a.token)
    log(f"[촬영] 임무 정보 기다림: {os.path.join(a.local, 'mission.json')}")
    Shooter(a, cam, th, veh, Mission(a.local)).loop()


if __name__ == "__main__":
    main()
