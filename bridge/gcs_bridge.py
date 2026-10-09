#!/usr/bin/env python3
"""
드론 수색 GCS 브리지 (PX4 전용) — 앱과 드론 사이를 잇습니다.

  패드의 GCS 앱  ⇄ WebSocket ⇄  gcs_bridge.py  ⇄ MAVLink ⇄  드론 (PX4)
    · 앱 → 드론 : 경로 업로드, 이륙·임무 시작, 복귀(RTL)
    · 드론 → 앱 : 위치·고도·배터리·모드·시동 상태 (0.5초마다), PX4 상태 메시지, 명령 결과

사용법
  1) 실기체 없이 시험
       python3 gcs_bridge.py --demo --lat 33.4996 --lng 126.5312

  2) 실기체 (지상 노트북 또는 패드의 Termux에서 실행)
       pip install pymavlink
       python3 gcs_bridge.py --mav udpin:0.0.0.0:14550          # UDP로 들어오는 텔레메트리
       python3 gcs_bridge.py --mav /dev/ttyUSB0,57600            # SiK 텔레메트리 무선 모뎀
     기체마다 MAV_SYS_ID를 1, 2, 3…으로 다르게 설정하세요 (최대 250). MAV_SYS_ID 번호 = 앱의 '드론 1, 2, 3…'.

  드론에 라즈베리파이를 달았을 때 (LTE로 텔레메트리 전달)
     PX4 TELEM2 ─(UART)─ 라즈베리파이 ─(LTE)─ 지상 브리지
       PX4 파라미터:  MAV_1_CONFIG = TELEM 2,  MAV_1_MODE = Onboard,  SER_TEL2_BAUD = 921600
       파이에서:      mavlink-routerd -e <지상 IP>:14550 /dev/serial0:921600
       지상에서:      python3 gcs_bridge.py --mav udpin:0.0.0.0:14550
     LTE는 통신사 NAT 때문에 지상으로 바로 들어오지 못하는 경우가 많습니다. 파이와 지상 기기를
     Tailscale 같은 VPN에 같이 넣고 <지상 IP> 자리에 VPN 주소를 쓰는 것이 가장 간단합니다.
     파이의 사진 촬영은 PX4가 보내는 CAMERA_TRIGGER 메시지에 맞춰 찍게 하세요.
       PX4 파라미터:  TRIG_INTERFACE = MAVLink,  TRIG_MODE = Distance (on command)

  3) 앱 연결: 설정 → 드론 실시간 위치 → '실시간' → 브리지 주소 → 연결
       같은 패드에서 실행(Termux 등):  ws://127.0.0.1:8766
       노트북 등 다른 기기에서 실행:   wss://<노트북 IP>:8766  (앱이 https이므로 TLS 필요)
     TLS 인증서 (한 번만):
       openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=gcs" \\
               -addext "subjectAltName=IP:<노트북 IP>"
       python3 gcs_bridge.py --mav udpin:0.0.0.0:14550 --cert cert.pem --key key.pem
     패드 Chrome에서 https://<노트북 IP>:8766 을 한 번 열어 '고급 → 계속'으로 인증서를 허용해 두세요.

업로드되는 임무 (기체별)
  이륙(NAV_TAKEOFF, 기체 현재 위치) → 속도(DO_CHANGE_SPEED) → 거리 간격 촬영 시작(DO_SET_CAM_TRIGG_DIST)
  → 웨이포인트들(NAV_WAYPOINT, 상대고도) → 촬영 중지 → 복귀(NAV_RETURN_TO_LAUNCH)

이륙 순서 (PX4): 시동(ARM) → MISSION 모드. PX4는 시동이 걸린 상태에서만 미션 모드로 들어가며,
  착륙 상태에서 미션 모드가 되면 TAKEOFF 항목부터 실행합니다. 시동 후 일정 시간(COM_DISARM_PRFLT)
  안에 이륙하지 않으면 PX4가 시동을 끄므로 두 명령을 곧바로 이어 보냅니다.

안전 한도 (어기면 업로드 거부): 고도 5~150m(국내 비행 고도 상한), 속도 1~15m/s,
  웨이포인트 600개 이하, 모든 점이 출발지 반경 5km 이내.
  실기체 첫 비행 전에 반드시 PX4 SITL(시뮬레이터)로 먼저 확인하고, 조종기로 즉시 수동 전환할 수 있게 준비하세요.
  QGroundControl을 함께 켜도 됩니다 (브리지는 MAVLink 시스템 번호 254를 써서 QGC의 255와 겹치지 않음).

메시지 형식 (JSON)
  드론→앱 {"type":"telemetry","id":1,"lat":..,"lng":..,"alt":상대고도m,"bat":%,"hdg":도,"spd":m/s,"mode":"MISSION","armed":true,"ts":기체시각(초)}
          {"type":"status","id":1,"text":"Preflight Fail: ...","sev":3}
          {"type":"detection","id":1,"lat":..,"lng":..,"alt":..,"temp":36.4,"uid":"..."}   (열화상 탐지를 브리지로 보낼 때, 보통은 Firebase로 올림)
  앱→드론 {"type":"upload","mission":"M..","drones":[{"id":1,"home":[lat,lng],"alt":100,"speed":8,"shot":39.1,"route":[[lat,lng],..]}]}
          {"type":"start","ids":[1,2]}   {"type":"rtl","ids":[1,2]}
  결과    {"type":"ack","op":"upload|start|rtl","id":1,"ok":true,"msg":"..."}

외부 패키지 없이 동작합니다 (실기체 연결에만 pymavlink 필요).
"""
import argparse, asyncio, base64, hashlib, json, math, queue, random, ssl, struct, sys, threading, time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LIMITS = {"alt": (5, 150), "speed": (1, 15), "points": 600, "radius": 5000}

lock = threading.Lock()
state = {}        # id -> 최신 텔레메트리
events = []       # 한 번만 보낼 메시지 (ack, 탐지)
clients = set()
vehicles = {}     # sysid -> {"conn", "autopilot", "armed"}
jobs = {}         # 연결 -> 작업 큐
demo = None       # 데모 모드 상태


def ack(op, sid, ok, msg):
    print(f"[{op}] 드론 {sid}: {'성공' if ok else '실패'} · {msg}")
    with lock:
        events.append({"type": "ack", "op": op, "id": sid, "ok": ok, "msg": msg})


# ======================= WebSocket 서버 (표준 라이브러리) =======================
def ws_frame(text):
    data = text.encode("utf-8")
    n = len(data)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    return head + data


async def ws_reader(reader):
    buf = b""
    while True:
        h = await reader.readexactly(2)
        fin, op, ln = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", await reader.readexactly(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", await reader.readexactly(8))[0]
        mask = await reader.readexactly(4) if h[1] & 0x80 else b"\0\0\0\0"
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(ln)))
        if op == 0x8:
            return
        if op in (0x0, 0x1):
            buf += data
            if fin:
                try:
                    on_app_message(json.loads(buf.decode("utf-8")))
                except Exception as e:
                    print("[앱 메시지 오류]", e)
                buf = b""


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        headers = {}
        for line in req.decode("latin-1").split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key")
        if not key:  # 브라우저로 주소를 직접 연 경우 (인증서 허용용)
            body = "드론 수색 GCS 브리지가 실행 중입니다. 이제 앱에서 이 주소로 연결하세요.".encode("utf-8")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        clients.add(writer)
        print(f"[앱 연결] {peer} (현재 {len(clients)}개)")
        await ws_reader(reader)
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        pass
    finally:
        if writer in clients:
            clients.discard(writer)
            print(f"[앱 연결 끊김] {peer} (현재 {len(clients)}개)")
        try:
            writer.close()
        except Exception:
            pass


async def broadcaster(interval):
    while True:
        await asyncio.sleep(interval)
        with lock:
            msgs = events[:] + [dict(v) for v in state.values()]   # 이벤트(지나간 웨이포인트 위치 등)가 현재 위치보다 먼저
            events.clear()
        if not msgs or not clients:
            continue
        frame = ws_frame(json.dumps(msgs, ensure_ascii=False))
        for w in list(clients):
            try:
                w.write(frame)
                await w.drain()
            except Exception:
                clients.discard(w)


# ======================= 앱 명령 처리 =======================
def check_route(d):
    """업로드 전 안전 검사. 문제가 있으면 이유(문자열), 없으면 None"""
    try:
        alt, spd, route = float(d["alt"]), float(d["speed"]), d["route"]
        hlat, hlng = map(float, d["home"])
    except Exception:
        return "임무 형식이 올바르지 않습니다"
    if not LIMITS["alt"][0] <= alt <= LIMITS["alt"][1]:
        return f"고도 {alt:.0f}m가 허용 범위({LIMITS['alt'][0]}~{LIMITS['alt'][1]}m)를 벗어납니다"
    if not LIMITS["speed"][0] <= spd <= LIMITS["speed"][1]:
        return f"속도 {spd}m/s가 허용 범위({LIMITS['speed'][0]}~{LIMITS['speed'][1]}m/s)를 벗어납니다"
    if not route:
        return "웨이포인트가 없습니다"
    if len(route) > LIMITS["points"]:
        return f"웨이포인트 {len(route)}개가 한도({LIMITS['points']}개)를 넘습니다"
    mlng = m_per_lng(hlat)
    for la, lo in route:
        if math.hypot((la - hlat) * 111320, (lo - hlng) * mlng) > LIMITS["radius"]:
            return f"출발지에서 {LIMITS['radius'] / 1000:.0f}km를 넘는 웨이포인트가 있습니다"
    return None


def on_app_message(m):
    if not isinstance(m, dict):
        return
    op = m.get("type")
    if op == "upload":
        print(f"[upload] 임무 {m.get('mission')} · 드론 {len(m.get('drones', []))}기")
        for d in m.get("drones", []):
            sid = int(d.get("id", 0))
            err = check_route(d)
            if err:
                ack("upload", sid, False, err)
            elif demo is not None:
                demo_upload(sid, d)
            else:
                dispatch(sid, ("upload", d))
    elif op in ("start", "rtl"):
        for sid in m.get("ids", []):
            sid = int(sid)
            if demo is not None:
                (demo_start if op == "start" else demo_rtl)(sid)
            else:
                dispatch(sid, (op, None))


def dispatch(sid, job):
    v = vehicles.get(sid)
    if not v:
        return ack(job[0], sid, False, "이 번호(SYSID)의 기체 신호가 없습니다")
    jobs[v["conn"]].put((sid, job))


# ======================= MAVLink (pymavlink, PX4) =======================
GCS_SYSID = 254                     # QGroundControl(255)과 겹치지 않게
MAX_SYSID = 250                     # 드론 번호로 쓸 수 있는 범위 (1~250)
MAV_TYPE_GCS, MAV_AUTOPILOT_PX4 = 6, 12
FRAME_MISSION, FRAME_REL_ALT = 2, 3  # QGC와 같은 프레임 (PX4는 INT/일반 둘 다 받음)
CMD = {"WAYPOINT": 16, "RTL": 20, "TAKEOFF": 22, "SET_MODE": 176, "SPEED": 178, "CAM_DIST": 206, "ARM": 400}
PX4_MODE = {"MISSION": (1, 4, 4), "RTL": (1, 4, 5), "LOITER": (1, 4, 3)}   # (base_mode, main_mode, sub_mode)
MAV_RESULT = {1: "일시 거부", 2: "거부", 3: "지원 안 함", 4: "실패", 5: "진행 중", 6: "취소됨"}
MISSION_RESULT = {1: "오류", 2: "지원 안 하는 프레임", 3: "지원 안 하는 명령", 4: "저장 공간 부족(SD카드 확인)",
                  5: "잘못된 값", 13: "순서 오류", 15: "거부됨"}
NAN = float("nan")                   # PX4: 방향(yaw) NaN = 현재 방향 유지


def tel(sid):
    """기체별 텔레메트리 항목 (없으면 만듦). lock 안에서 부름"""
    return state.setdefault(sid, {"type": "telemetry", "id": sid})


def m_per_lng(lat):
    """그 위도에서 경도 1도의 길이(m)"""
    return 111320 * math.cos(math.radians(lat))


def handle_msg(conn, msg, mavutil):
    sid = msg.get_srcSystem()
    if not 1 <= sid <= MAX_SYSID:
        return
    t = msg.get_type()
    with lock:
        if t == "HEARTBEAT":
            if msg.type == MAV_TYPE_GCS:
                return
            armed = bool(msg.base_mode & 128)
            vehicles[sid] = {"conn": conn, "autopilot": msg.autopilot, "armed": armed}
            d = tel(sid)
            d["armed"] = armed
            try:
                d["mode"] = mavutil.mode_string_v10(msg)
            except Exception:
                pass
        elif t == "GLOBAL_POSITION_INT":
            d = tel(sid)
            d.update(lat=msg.lat / 1e7, lng=msg.lon / 1e7, alt=round(msg.relative_alt / 1000, 1),
                     spd=round(math.hypot(msg.vx, msg.vy) / 100, 1), ts=msg.time_boot_ms / 1000)
            if msg.hdg != 65535:
                d["hdg"] = msg.hdg / 100
        elif t in ("SYS_STATUS", "BATTERY_STATUS") and msg.battery_remaining >= 0:
            tel(sid)["bat"] = msg.battery_remaining
        elif t == "STATUSTEXT":    # 사전 점검 실패 등 PX4 안내 문구
            text = msg.text.decode("utf-8", "replace") if isinstance(msg.text, bytes) else str(msg.text)
            events.append({"type": "status", "id": sid, "text": text.strip("\0 ").strip(), "sev": msg.severity})


def wait_for(conn, sid, types, mavutil, pred=None, timeout=4.0):
    """원하는 메시지가 올 때까지 기다리면서 그 사이 텔레메트리도 계속 갱신"""
    end = time.time() + timeout
    while time.time() < end:
        msg = conn.recv_match(blocking=True, timeout=0.3)
        if msg is None:
            continue
        handle_msg(conn, msg, mavutil)
        if msg.get_type() in types and msg.get_srcSystem() == sid and (pred is None or pred(msg)):
            return msg
    return None


def build_items(d):
    hlat, hlng = map(float, d["home"])
    alt, spd, shot = float(d["alt"]), float(d["speed"]), float(d.get("shot", 0))
    items = [(FRAME_REL_ALT, CMD["TAKEOFF"], 0, 0, 0, NAN, hlat, hlng, alt),      # (frame, cmd, p1~p4, lat, lng, alt)
             (FRAME_MISSION, CMD["SPEED"], 1, spd, -1, 0, 0, 0, 0)]               # 1 = 대지 속도
    if shot > 0:
        items.append((FRAME_MISSION, CMD["CAM_DIST"], shot, 0, 1, 0, 0, 0, 0))    # shot m마다 촬영, 시작 즉시 1장
    items += [(FRAME_REL_ALT, CMD["WAYPOINT"], 0, 0, 0, NAN, float(la), float(lo), alt) for la, lo in d["route"]]
    if shot > 0:
        items.append((FRAME_MISSION, CMD["CAM_DIST"], 0, 0, 0, 0, 0, 0, 0))       # 촬영 중지
    items.append((FRAME_MISSION, CMD["RTL"], 0, 0, 0, 0, 0, 0, 0))
    return items


def job_upload(conn, sid, d, mavutil):
    items = build_items(d)
    n = len(items)
    for attempt in range(3):
        conn.mav.mission_count_send(sid, 1, n, 0)
        sent = set()
        while True:
            msg = wait_for(conn, sid, ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"), mavutil, timeout=3)
            if msg is None:
                break                                   # 응답 없음 → 처음부터 다시
            if msg.get_type() == "MISSION_ACK":
                if msg.type == 0 and len(sent) == n:
                    return True, f"{n}개 항목 업로드 완료 (웨이포인트 {len(d['route'])}개)"
                if msg.type != 0:
                    return False, f"PX4가 임무를 거부했습니다 ({MISSION_RESULT.get(msg.type, msg.type)})"
                continue
            seq = msg.seq
            if not 0 <= seq < n:
                continue
            fr, cmd, p1, p2, p3, p4, la, lo, al = items[seq]
            if msg.get_type() == "MISSION_REQUEST_INT":   # 요청한 형식 그대로 답함
                conn.mav.mission_item_int_send(sid, 1, seq, fr, cmd, 0, 1, p1, p2, p3, p4,
                                               int(round(la * 1e7)), int(round(lo * 1e7)), al, 0)
            else:
                conn.mav.mission_item_send(sid, 1, seq, fr, cmd, 0, 1, p1, p2, p3, p4, la, lo, al, 0)
            sent.add(seq)
        print(f"[upload] 드론 {sid}: 응답 없음, 재시도 {attempt + 1}/3")
    return False, "기체 응답 없음 (3회 시도)"


def command(conn, sid, cmd, mavutil, *p):
    p = list(p) + [0] * (7 - len(p))
    conn.mav.command_long_send(sid, 1, cmd, 0, *p)
    a = wait_for(conn, sid, ("COMMAND_ACK",), mavutil, pred=lambda x: x.command == cmd, timeout=4)
    if a is None:
        return False, "응답 없음"
    return a.result == 0, MAV_RESULT.get(a.result, str(a.result))


def set_mode(conn, sid, name, mavutil):
    base, main, sub = PX4_MODE[name]
    return command(conn, sid, CMD["SET_MODE"], mavutil, base, main, sub)


def job_start(conn, sid, mavutil):
    ok, r = command(conn, sid, CMD["ARM"], mavutil, 1)
    if not ok:
        return False, f"시동 거부 ({r}) · PX4 사전 점검 메시지를 확인하세요 (GPS, 배터리, 안전 스위치, 원격 조종기)"
    ok, r = set_mode(conn, sid, "MISSION", mavutil)
    if not ok:
        command(conn, sid, CMD["ARM"], mavutil, 0)     # 미션 모드 실패 시 시동을 다시 끔
        return False, f"미션 모드 전환 실패 ({r}) · 시동을 껐습니다"
    return True, "시동 · 미션 모드 · 이륙"


def job_rtl(conn, sid, mavutil):
    ok, r = set_mode(conn, sid, "RTL", mavutil)
    return (True, "복귀 모드로 전환") if ok else (False, f"복귀 전환 실패 ({r})")


JOBS = {"upload": lambda c, s, d, mu: job_upload(c, s, d, mu),
        "start": lambda c, s, d, mu: job_start(c, s, mu),
        "rtl": lambda c, s, d, mu: job_rtl(c, s, mu)}


def mav_worker(conn_str):
    try:
        from pymavlink import mavutil
    except ImportError:
        sys.exit("pymavlink가 없습니다. 'pip install pymavlink' 후 다시 실행하세요.")
    kw = {"source_system": GCS_SYSID}
    if "," in conn_str and not conn_str.startswith(("udp", "tcp")):
        dev, baud = conn_str.split(",", 1)
        conn = mavutil.mavlink_connection(dev, baud=int(baud), **kw)
    else:
        conn = mavutil.mavlink_connection(conn_str, **kw)
    jobs[conn] = queue.Queue()
    print(f"[MAVLink] {conn_str} 수신 대기")
    last_hb = 0
    while True:
        if time.time() - last_hb > 1:   # GCS 하트비트 (PX4 데이터 링크 끊김 페일세이프 판단에 쓰임)
            conn.mav.heartbeat_send(MAV_TYPE_GCS, 8, 0, 0, 0)
            last_hb = time.time()
        try:
            sid, (op, data) = jobs[conn].get_nowait()
            if vehicles.get(sid, {}).get("autopilot") != MAV_AUTOPILOT_PX4:
                ok, msg = False, "PX4 기체가 아닙니다 (이 브리지는 PX4 전용)"
            else:
                try:
                    ok, msg = JOBS[op](conn, sid, data, mavutil)
                except Exception as e:
                    ok, msg = False, f"오류: {e}"
            ack(op, sid, ok, msg)
        except queue.Empty:
            pass
        msg = conn.recv_match(blocking=True, timeout=0.2)
        if msg is not None:
            handle_msg(conn, msg, mavutil)


# ======================= 데모 드론 =======================
def demo_upload(sid, d):
    with lock:
        la, lo = demo["home"]
        mlng = m_per_lng(la)
        pts = [(0.0, 0.0)] + [((p[1] - lo) * mlng, (p[0] - la) * 111320) for p in d["route"]] + [(0.0, 0.0)]
        demo["drones"][sid] = {"route": pts, "t0": None, "speed": float(d["speed"]), "alt": float(d["alt"]),
                               "rtl": False, "prev": 0.0, "flown": 0.0, "sent": 0}
    ack("upload", sid, True, f"데모: 웨이포인트 {len(d['route'])}개 업로드 완료")


def demo_start(sid):
    with lock:
        v = demo["drones"].get(sid)
        ok = bool(v and v.get("route") and v["t0"] is None)
        if ok:
            v["t0"] = time.time()
    ack("start", sid, ok, "데모: 시동 · 미션 모드 · 이륙" if ok else "업로드된 경로가 없거나 이미 비행 중입니다")


def demo_rtl(sid):
    with lock:
        v = demo["drones"].get(sid)
        ok = bool(v and v.get("t0") and not v.get("done") and v.get("pos"))
        if ok:
            v.update(route=[v["pos"], (0.0, 0.0)], t0=time.time(), rtl=True, prev=v["prev"] + v["flown"], flown=0.0, sent=0)
    ack("rtl", sid, ok, "데모: 복귀 모드로 전환" if ok else "비행 중이 아닙니다")


def demo_worker(n, accel):
    with lock:
        for k in range(1, n + 1):
            demo["drones"][k] = {"route": None, "t0": None}
    while True:
        out, now = {}, time.time()
        with lock:
            la, lo = demo["home"]
            mlng = m_per_lng(la)
            for k, v in demo["drones"].items():
                tel = {"type": "telemetry", "id": k, "lat": la, "lng": lo, "alt": 0.0, "bat": 100.0, "hdg": 0.0,
                       "spd": 0, "mode": "STANDBY", "armed": False, "ts": round(now * accel, 2)}
                if v.get("route") and v.get("t0") is not None:
                    # 실제 PX4처럼: 제자리 수직 이륙 → 임무 고도로 경로 비행 → 출발지 위에서 수직 착륙 (복귀 명령이면 이륙 단계 없음)
                    pts, spd, amax = v["route"], v["speed"], v["alt"]
                    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
                    up = 0.0 if v["rtl"] else amax
                    total, eff = sum(seg), (now - v["t0"]) * accel * spd
                    dd, i = min(max(eff - up, 0.0), total), 0
                    while i < len(seg) - 1 and dd > seg[i]:
                        dd -= seg[i]
                        i += 1
                    a, b = pts[i], pts[i + 1]
                    f = dd / seg[i] if seg[i] else 1
                    x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                    landed = eff >= up + total + amax
                    alt = 0.0 if landed else (eff if eff < up else amax if eff < up + total else up + total + amax - eff)
                    v["flown"] = min(eff, up + total + amax)
                    # 지나간 웨이포인트 위치도 보냄 (배속 시험에서 한 번에 수십 m씩 움직여도 모서리를 건너뛰지 않게)
                    passed = i if eff - up < total else len(pts) - 1
                    cum = 0.0
                    for j in range(1, passed + 1):
                        cum += seg[j - 1]
                        if j > v.get("sent", 0):
                            events.append({"type": "telemetry", "id": k, "lat": round(la + pts[j][1] / 111320, 7), "lng": round(lo + pts[j][0] / mlng, 7),
                                           "alt": amax, "spd": spd, "mode": "MISSION", "armed": True,
                                           "ts": round(v["t0"] * accel + (up + cum) / spd, 2)})
                    v["sent"] = max(v.get("sent", 0), passed)
                    v["pos"], v["done"] = (x, y), landed
                    tel.update(lat=round(la + y / 111320, 7), lng=round(lo + x / mlng, 7), alt=round(alt, 1),
                               bat=round(max(5.0, 100 - (v["prev"] + v["flown"]) / spd / 12), 1),
                               hdg=round((math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360) % 360, 1),
                               spd=0 if landed else spd, armed=not landed,
                               mode="LAND" if landed or eff >= up + total else ("TAKEOFF" if eff < up else "RTL" if v["rtl"] or i == len(seg) - 1 else "MISSION"))
                    if not landed and alt > 50 and random.random() < 0.15 * accel / 600:   # 데모 열화상 탐지
                        fire = random.random() < 0.25
                        temp = random.uniform(55, 220) if fire else random.uniform(33.0, 37.5)   # 34°C 미만은 앱이 거름
                        demo["uid"] = demo.get("uid", 0) + 1
                        events.append({"type": "detection", "id": k, "lat": tel["lat"], "lng": tel["lng"], "alt": tel["alt"],
                                       "temp": round(temp, 1), "uid": f"demo-{demo['uid']}"})
                out[k] = tel
            state.update(out)
        time.sleep(0.1)


async def main():
    global demo
    ap = argparse.ArgumentParser(description="GCS 앱 ⇄ PX4 드론(MAVLink) 브리지")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--mav", action="append", default=[], help="MAVLink 연결 (여러 번 지정 가능)")
    ap.add_argument("--demo", action="store_true", help="실기체 없이 데모 드론으로 시험")
    ap.add_argument("--lat", type=float, default=33.4996, help="데모 출발지 위도")
    ap.add_argument("--lng", type=float, default=126.5312, help="데모 출발지 경도")
    ap.add_argument("--drones", type=int, default=4, help="데모 드론 수")
    ap.add_argument("--accel", type=float, default=1, help="데모 시간 배속 (시험용)")
    ap.add_argument("--rate", type=float, default=2, help="앱으로 보내는 횟수(초당)")
    ap.add_argument("--cert")
    ap.add_argument("--key")
    a = ap.parse_args()
    if a.demo == bool(a.mav):
        ap.error("--demo 또는 --mav 중 하나만 지정하세요.")
    if a.demo:
        demo = {"home": (a.lat, a.lng), "drones": {}}
        threading.Thread(target=demo_worker, args=(max(1, a.drones), a.accel), daemon=True).start()
    for c in a.mav:
        threading.Thread(target=mav_worker, args=(c,), daemon=True).start()
    ctx = None
    if a.cert and a.key:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(a.cert, a.key)
    server = await asyncio.start_server(handle, a.host, a.port, ssl=ctx)
    print(f"[브리지] {'wss' if ctx else 'ws'}://{a.host}:{a.port} 에서 앱 연결 대기 ({'데모' if a.demo else '실기체'})")
    asyncio.create_task(broadcaster(1 / a.rate))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[브리지] 종료")
