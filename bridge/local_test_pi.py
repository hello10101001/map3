#!/usr/bin/env python3
"""
Wi-Fi 사진 허브 시험용 '가짜 촬영 프로그램' — 실제 파이·카메라 없이 앱에서 사진·탐지가 보이는지 확인합니다.

  python3 gcs_bridge.py --demo --drones 1 --local ./local --token pw     # 브리지 (데모 드론)
  python3 local_test_pi.py ./local [초당 사진 수] [촬영 시작까지 초]       # 이 프로그램

앱에서 경로를 업로드하면 브리지가 ./local/mission.json 을 쓰고, 이 프로그램이 그 경로를 따라
촬영 간격(shot)마다 가짜 사진을 만들어 ./local/out/<임무>/photos.jsonl 에, 가끔 탐지를 detections.jsonl 에 씁니다.
실제 촬영 프로그램(pi_drone.py --local)이 쓰는 형식과 같습니다.
"""
import io, json, math, os, sys, time

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "./local")
RATE = float(sys.argv[2]) if len(sys.argv) > 2 else 10      # 초당 사진 수 (시험이라 빠르게)
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 10     # 업로드 뒤 촬영 시작까지 초 (그 사이 앱에서 '임무 시작')
M_LAT = 111320

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None


def jpeg(text, hot=False):
    if Image is None:   # PIL이 없으면 1×1 회색 JPEG
        import base64
        return base64.b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
    im = Image.new("RGB", (320, 240), (40, 90, 50) if not hot else (20, 20, 60))
    d = ImageDraw.Draw(im)
    for i in range(0, 320, 40):
        d.line([(i, 0), (i, 240)], fill=(60, 110, 70))
    if hot:
        d.ellipse([140, 100, 180, 140], fill=(255, 80, 40))
    d.rectangle([0, 215, 320, 240], fill=(0, 0, 0))
    d.text((6, 220), text, fill=(255, 255, 255))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=70)
    return b.getvalue()


def cell_of(g, lat, lng):
    x = (lng - g["lng0"]) * M_LAT * math.cos(math.radians(g["lat0"]))
    y = (lat - g["lat0"]) * M_LAT
    c, r = math.floor((x - g["x0"]) / g["cw"]), math.floor((y - g["y0"]) / g["ch"])
    return r * g["cols"] + c if 0 <= c < g["cols"] and 0 <= r < g["rows"] else None


def shots(route, cam, shot):
    """경로의 촬영 구간(cam)을 따라 shot m마다 한 점"""
    pts = route[cam[0]:cam[1] + 1]
    out, carry = [], 0.0
    for a, b in zip(pts, pts[1:]):
        k = M_LAT * math.cos(math.radians(a[0]))
        L = math.hypot((b[0] - a[0]) * M_LAT, (b[1] - a[1]) * k)
        s = carry
        while s <= L:
            f = s / L if L else 0
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] if len(a) > 2 else 50))
            s += shot
        carry = s - L
    return out


def run(doc):
    mid, g = doc["mission"], doc["grid"]
    print(f"[가짜 촬영] 임무 {mid}: {DELAY:.0f}초 뒤 촬영 시작 (앱에서 '임무 시작'을 누르세요)")
    time.sleep(DELAY)
    for sid, d in doc["drones"].items():
        base = os.path.join(ROOT, "out", mid, sid)
        os.makedirs(os.path.join(base, "thumb"), exist_ok=True)
        os.makedirs(os.path.join(base, "thermal"), exist_ok=True)
        cam = d.get("cam") or [0, len(d["route"]) - 1]
        pts = shots(d["route"], cam, float(d.get("shot") or 30))
        print(f"[가짜 촬영] 임무 {mid} 드론 {sid}: 사진 {len(pts)}장")
        for i, (la, lo, z) in enumerate(pts):
            name = f"{int(time.time() * 1000)}_{i:04d}"
            now = int(time.time() * 1000)
            cell = cell_of(g, la, lo)
            with open(os.path.join(base, "thumb", name + ".jpg"), "wb") as f:
                f.write(jpeg(f"drone {sid} #{i} cell {cell}"))
            p = {"id": f"{sid}-{name}", "drone": int(sid), "lat": round(la, 7), "lng": round(lo, 7), "alt": z, "hdg": 0, "takenAt": now,
                 "cell": cell, "tempAvgC": round(14 + 3 * math.sin(i / 7), 2), "thumb": f"{mid}/{sid}/thumb/{name}.jpg", "file": f"{mid}/{sid}/thumb/{name}.jpg"}
            with open(os.path.join(ROOT, "out", mid, "photos.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(p) + "\n")
            if i % 25 == 12:
                with open(os.path.join(base, "thermal", name + "_det.jpg"), "wb") as f:
                    f.write(jpeg(f"det {sid} #{i}", hot=True))
                det = {"id": f"{sid}-{name}-d", "drone": int(sid), "lat": round(la, 7), "lng": round(lo, 7), "alt": z, "tempC": 36.8,
                       "takenAt": now, "cell": cell, "img": f"{mid}/{sid}/thermal/{name}_det.jpg"}
                with open(os.path.join(ROOT, "out", mid, "detections.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(det) + "\n")
            time.sleep(1 / RATE)


if __name__ == "__main__":
    print(f"[가짜 촬영] {ROOT}/mission.json 을 기다립니다")
    seen = None
    while True:
        try:
            t = os.path.getmtime(os.path.join(ROOT, "mission.json"))
            if t != seen:
                seen = t
                with open(os.path.join(ROOT, "mission.json"), encoding="utf-8") as f:
                    run(json.load(f))
        except FileNotFoundError:
            pass
        time.sleep(0.5)
