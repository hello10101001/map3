/*
 * 텔레메트리 무선기(USB)로 PX4 드론과 직접 통신 — 브리지(gcs_bridge.py) 없이 태블릿 혼자.
 *
 *   태블릿 앱 ─ USB(OTG) ─ 텔레메트리 무선기 ⇄ (무선 1~2km) ⇄ 무선기 ─ 픽스호크 TELEM1
 *
 * 앱에는 브리지와 똑같은 메시지(JSON)를 주고받는 WebSocket 모양의 객체로 보임:
 *   new UsbMavLink({baud}) → onopen / onmessage({data}) / onclose({reason}) / send(JSON 문자열) / close()
 *   드론→앱 telemetry · status · detection · ack,  앱→드론 upload · start · rtl
 *
 * USB 읽기·쓰기 방법 (있는 것부터)
 *   1) 안드로이드 앱: window.GCSAndroid.usbOpen/usbWrite/usbClose, 받은 데이터는 window.__usbData(base64)
 *   2) PC 크롬: Web Serial (navigator.serial) — 브라우저에서 시험할 때
 *   (시험용: window.__usbBackend = {open(baud, onData, onState), write(Uint8Array), close()})
 *
 * 탐지 알림 (라즈베리파이 → 픽스호크 → 무선기): STATUSTEXT 한 줄
 *   "DET <번호> <위도> <경도> <온도°C>"   예) "DET d17 33.4512345 126.5712345 36.8"
 *   PX4에서 무선기 포트·파이 포트 모두 MAV_0_FORWARD = 1, MAV_1_FORWARD = 1 (메시지 중계)로 켜야 무선기로 넘어옴
 *
 * MAVLink 2로 보내고, 받을 때는 1·2 모두 읽음. 메시지 배치·CRC_EXTRA는 MAVLink common.xml 기준.
 */
(function () {
  "use strict";
  const GCS_SYS = 254, GCS_COMP = 190;               // QGroundControl(255)과 겹치지 않게 (브리지와 같음)
  const STALE_MS = 3000;                              // 이만큼 신호가 없으면 위치를 앱에 보내지 않음 → 앱이 '신호 없음'
  const AP_INVALID = 8, MAV_TYPE_GCS = 6, AP_PX4 = 12;
  const LIMITS = { alt: [5, 150], speed: [1, 15], points: 600, radius: 5000, rel: [-300, 600] };
  const CMD = { WAYPOINT: 16, RTL: 20, TAKEOFF: 22, SET_MODE: 176, SPEED: 178, CAM_DIST: 206, ARM: 400 };
  const FRAME_MISSION = 2, FRAME_REL_ALT = 3;
  const PX4_MODE = { MISSION: [1, 4, 4], RTL: [1, 4, 5] };
  const MAV_RESULT = { 1: "일시 거부", 2: "거부", 3: "지원 안 함", 4: "실패", 5: "진행 중", 6: "취소됨" };
  const MISSION_RESULT = { 1: "오류", 2: "지원 안 하는 프레임", 3: "지원 안 하는 명령", 4: "저장 공간 부족(SD카드 확인)", 5: "잘못된 값", 13: "순서 오류", 15: "거부됨" };
  // 메시지 번호 → CRC_EXTRA (common.xml)
  const EXTRA = { 0: 50, 1: 124, 24: 24, 33: 104, 39: 254, 40: 230, 42: 28, 44: 221, 47: 153, 51: 196, 73: 38, 76: 152, 77: 143, 147: 154, 253: 83 };
  // PX4 custom_mode → 모드 이름 (pymavlink와 같은 이름)
  const MAIN = { 1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 5: "ACRO", 6: "OFFBOARD", 7: "STABILIZED", 8: "RATTITUDE" };
  const AUTO = { 1: "READY", 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL", 6: "LAND", 7: "RTGS", 8: "FOLLOWME", 9: "PRECLAND" };

  function x25(bytes, crc) {
    crc = crc == null ? 0xffff : crc;
    for (let i = 0; i < bytes.length; i++) {
      let t = (bytes[i] ^ (crc & 0xff)) & 0xff;
      t = (t ^ (t << 4)) & 0xff;
      crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xffff;
    }
    return crc;
  }
  const b64enc = u8 => { let s = ""; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); };
  const b64dec = s => { const b = atob(s), u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; };
  const mPerLng = lat => 111320 * Math.cos(lat * Math.PI / 180);

  /* ---------- 경로 → PX4 임무 항목 (브리지 gcs_bridge.py와 같은 규칙) ---------- */
  function routeOf(d) {
    const alt = +d.alt, pts = d.route.map(p => [+p[0], +p[1], p.length > 2 ? +p[2] : alt]);
    const cam = d.cam || [0, pts.length - 1];
    return { pts, takeoff: d.takeoff != null ? +d.takeoff : alt, cam: [cam[0] | 0, cam[1] | 0] };
  }
  function checkRoute(d) {
    let alt, spd, home, r;
    try {
      alt = +d.alt; spd = +d.speed; home = d.home.map(Number); r = routeOf(d);
      if (![alt, spd, home[0], home[1]].every(isFinite)) throw 0;
    } catch (e) { return "임무 형식이 올바르지 않습니다"; }
    if (alt < LIMITS.alt[0] || alt > LIMITS.alt[1]) return `고도 ${alt.toFixed(0)}m가 허용 범위(${LIMITS.alt[0]}~${LIMITS.alt[1]}m)를 벗어납니다`;
    if (spd < LIMITS.speed[0] || spd > LIMITS.speed[1]) return `속도 ${spd}m/s가 허용 범위(${LIMITS.speed[0]}~${LIMITS.speed[1]}m/s)를 벗어납니다`;
    if (!r.pts.length) return "웨이포인트가 없습니다";
    if (r.pts.length > LIMITS.points) return `웨이포인트 ${r.pts.length}개가 한도(${LIMITS.points}개)를 넘습니다`;
    if (!(alt <= r.takeoff && r.takeoff <= LIMITS.rel[1])) return `이륙 고도 ${r.takeoff.toFixed(0)}m가 허용 범위(${alt.toFixed(0)}~${LIMITS.rel[1]}m)를 벗어납니다`;
    if (!(0 <= r.cam[0] && r.cam[0] <= r.cam[1] && r.cam[1] < r.pts.length)) return "촬영 시작·끝 웨이포인트 번호가 올바르지 않습니다";
    const k = mPerLng(home[0]);
    for (const [la, lo, z] of r.pts) {
      if (Math.hypot((la - home[0]) * 111320, (lo - home[1]) * k) > LIMITS.radius) return `출발지에서 ${LIMITS.radius / 1000}km를 넘는 웨이포인트가 있습니다`;
      if (z < LIMITS.rel[0] || z > LIMITS.rel[1]) return `웨이포인트 고도 ${z.toFixed(0)}m(출발지 대비)가 허용 범위(${LIMITS.rel[0]}~${LIMITS.rel[1]}m)를 벗어납니다`;
    }
    return null;
  }
  /* 항목 = [frame, command, p1, p2, p3, p4, lat, lng, alt] */
  function buildItems(d) {
    const home = d.home.map(Number), spd = +d.speed, shot = +(d.shot || 0), r = routeOf(d);
    const items = [[FRAME_REL_ALT, CMD.TAKEOFF, 0, 0, 0, NaN, home[0], home[1], r.takeoff],   // 제자리 이륙 (방향 NaN = 그대로)
      [FRAME_MISSION, CMD.SPEED, 1, spd, -1, 0, 0, 0, 0]];                                  // 대지 속도
    r.pts.forEach(([la, lo, z], i) => {
      items.push([FRAME_REL_ALT, CMD.WAYPOINT, 0, 0, 0, NaN, la, lo, z]);
      if (shot > 0 && i === r.cam[0]) items.push([FRAME_MISSION, CMD.CAM_DIST, shot, 0, 1, 0, 0, 0, 0]);   // 촬영 시작 (파이가 직접 찍으면 무시됨)
      if (shot > 0 && i === r.cam[1]) items.push([FRAME_MISSION, CMD.CAM_DIST, 0, 0, 0, 0, 0, 0, 0]);
    });
    items.push([FRAME_MISSION, CMD.RTL, 0, 0, 0, 0, 0, 0, 0]);
    return items;
  }

  /* ---------- MAVLink 프레임 ---------- */
  class Packer {
    constructor() { this.seq = 0; }
    /* MAVLink 2 프레임. payload = 메시지 전체 길이(뒤쪽 0은 잘라서 보냄) */
    frame(msgid, payload) {
      let n = payload.length;
      while (n > 1 && payload[n - 1] === 0) n--;
      const f = new Uint8Array(10 + n + 2);
      f[0] = 0xFD; f[1] = n; f[2] = 0; f[3] = 0; f[4] = this.seq = (this.seq + 1) & 0xff; f[5] = GCS_SYS; f[6] = GCS_COMP;
      f[7] = msgid & 0xff; f[8] = (msgid >> 8) & 0xff; f[9] = (msgid >> 16) & 0xff;
      f.set(payload.subarray(0, n), 10);
      let crc = x25(f.subarray(1, 10 + n));
      crc = x25([EXTRA[msgid]], crc);
      f[10 + n] = crc & 0xff; f[11 + n] = crc >> 8;
      return f;
    }
    heartbeat() {
      const p = new Uint8Array(9), v = new DataView(p.buffer);
      v.setUint32(0, 0, true); p[4] = MAV_TYPE_GCS; p[5] = AP_INVALID; p[6] = 0; p[7] = 4; p[8] = 3;
      return this.frame(0, p);
    }
    missionCount(sys, comp, count) {
      const p = new Uint8Array(5), v = new DataView(p.buffer);
      v.setUint16(0, count, true); p[2] = sys; p[3] = comp; p[4] = 0;
      return this.frame(44, p);
    }
    missionItem(sys, comp, seq, it, asInt) {
      const p = new Uint8Array(38), v = new DataView(p.buffer);
      const [fr, cmd, p1, p2, p3, p4, la, lo, al] = it;
      v.setFloat32(0, p1, true); v.setFloat32(4, p2, true); v.setFloat32(8, p3, true); v.setFloat32(12, p4, true);
      if (asInt) { v.setInt32(16, Math.round(la * 1e7), true); v.setInt32(20, Math.round(lo * 1e7), true); }
      else { v.setFloat32(16, la, true); v.setFloat32(20, lo, true); }
      v.setFloat32(24, al, true); v.setUint16(28, seq, true); v.setUint16(30, cmd, true);
      p[32] = sys; p[33] = comp; p[34] = fr; p[35] = 0; p[36] = 1; p[37] = 0;
      return this.frame(asInt ? 73 : 39, p);
    }
    commandLong(sys, comp, cmd, params) {
      const p = new Uint8Array(33), v = new DataView(p.buffer);
      for (let i = 0; i < 7; i++) v.setFloat32(i * 4, params[i] || 0, true);
      v.setUint16(28, cmd, true); p[30] = sys; p[31] = comp; p[32] = 0;
      return this.frame(76, p);
    }
  }

  /* 받은 바이트에서 MAVLink 1·2 프레임을 꺼냄 (CRC가 맞는 것만, 모르는 메시지는 건너뜀) */
  class Parser {
    constructor() { this.buf = new Uint8Array(0); this.bad = 0; }
    push(bytes, out) {
      const b = new Uint8Array(this.buf.length + bytes.length);
      b.set(this.buf); b.set(bytes, this.buf.length);
      let i = 0;
      while (i < b.length) {
        const stx = b[i];
        if (stx !== 0xFD && stx !== 0xFE) { i++; continue; }
        if (b.length - i < 2) break;
        const len = b[i + 1], v2 = stx === 0xFD;
        const head = v2 ? 10 : 6, sig = v2 && b.length - i > 2 && (b[i + 2] & 1) ? 13 : 0;
        const total = head + len + 2 + sig;
        if (b.length - i < total) break;
        const msgid = v2 ? b[i + 7] | (b[i + 8] << 8) | (b[i + 9] << 16) : b[i + 5];
        const ex = EXTRA[msgid];
        if (ex === undefined) { i += total; continue; }   // 쓰지 않는 메시지
        let crc = x25(b.subarray(i + 1, i + head + len));
        crc = x25([ex], crc);
        if ((b[i + head + len] | (b[i + head + len + 1] << 8)) !== crc) { this.bad++; i++; continue; }   // 깨진 프레임: 한 바이트 넘기고 다시 찾음
        const pay = new Uint8Array(64);                    // MAVLink 2는 뒤쪽 0을 잘라 보냄 → 0으로 채워 읽음
        pay.set(b.subarray(i + head, i + head + Math.min(len, 64)));
        out.push({ id: msgid, sys: v2 ? b[i + 5] : b[i + 3], comp: v2 ? b[i + 6] : b[i + 4], p: pay, v: new DataView(pay.buffer), len });
        i += total;
      }
      this.buf = b.slice(i);
      if (this.buf.length > 4096) this.buf = this.buf.slice(-280);   // 이상한 데이터가 쌓이지 않게
    }
  }

  /* ---------- USB 읽기·쓰기 ---------- */
  function makeBackend() {
    if (window.__usbBackend) return window.__usbBackend;
    const A = window.GCSAndroid;
    if (A && A.usbOpen) {
      return {
        open(baud, onData, onState) {
          window.__usbData = s => { try { onData(b64dec(s)); } catch (e) { } };
          window.__usbState = (st, msg) => onState(st, msg);
          A.usbOpen(baud | 0);
        },
        write(u8) { return A.usbWrite(b64enc(u8)); },
        close() { window.__usbData = window.__usbState = null; try { A.usbClose(); } catch (e) { } }
      };
    }
    if (navigator.serial) {   // PC 크롬에서 시험
      let port = null, reader = null, closed = false;
      return {
        async open(baud, onData, onState) {
          try {
            const prev = await navigator.serial.getPorts();
            port = prev[0] || await navigator.serial.requestPort();
            await port.open({ baudRate: baud });
            onState("open", `Web Serial · ${baud}bps`);
            while (!closed && port.readable) {
              reader = port.readable.getReader();
              try { for (;;) { const { value, done } = await reader.read(); if (done) break; if (value) onData(value); } }
              finally { reader.releaseLock(); }
            }
          } catch (e) { if (!closed) onState("error", "시리얼 포트를 열지 못했습니다: " + (e.message || e)); }
        },
        write(u8) { if (!port || !port.writable) return false; const w = port.writable.getWriter(); w.write(u8).finally(() => w.releaseLock()); return true; },
        close() { closed = true; try { reader && reader.cancel(); } catch (e) { } try { port && port.close(); } catch (e) { } }
      };
    }
    return null;
  }

  /* ---------- 앱에 보이는 연결 객체 ---------- */
  class UsbMavLink {
    constructor(opt) {
      this.readyState = 0;
      this.baud = (opt && opt.baud) || 57600;
      this.onopen = this.onmessage = this.onclose = null;
      this.pk = new Packer(); this.ps = new Parser();
      this.veh = {};          // 기체 번호 → {comp, autopilot, seen, tel{}}
      this.waiters = [];      // 응답 기다리는 작업
      this.queue = {};        // 기체별 작업 순서 (업로드 → 이륙이 섞이지 않게)
      this.events = [];
      this.be = makeBackend();
      if (!this.be) { setTimeout(() => this._closed("이 기기에서는 USB 무선기를 쓸 수 없습니다 (안드로이드 앱 또는 PC 크롬에서 가능)"), 0); return; }
      this.be.open(this.baud, u8 => this._data(u8), (st, msg) => this._state(st, msg));
    }
    _state(st, msg) {
      if (st === "open") {
        this.readyState = 1;
        this.hb = setInterval(() => this._write(this.pk.heartbeat()), 1000);   // GCS 하트비트 (PX4 데이터 링크 끊김 판단)
        this.tick = setInterval(() => this._flush(), 250);
        this._write(this.pk.heartbeat());
        this.onopen && this.onopen();
        this.events.push({ type: "status", id: 0, text: `텔레메트리 무선기 연결 (${msg})` });
      } else if (st === "error" || st === "closed") {
        this._closed(msg);
      } else if (this.onwait) this.onwait(msg);
    }
    _closed(reason) {
      if (this.readyState === 3) return;
      this.readyState = 3;
      clearInterval(this.hb); clearInterval(this.tick);
      this.waiters.forEach(w => w.done(null)); this.waiters = [];
      try { this.be && this.be.close(); } catch (e) { }
      this.onclose && this.onclose({ reason: reason || "연결 끊김" });
    }
    close() { this._closed("연결 끊음"); }
    _write(f) { try { return this.be.write(f) !== false; } catch (e) { return false; } }
    _emit(msgs) { if (msgs.length && this.onmessage) this.onmessage({ data: JSON.stringify(msgs) }); }
    _flush() {
      const now = Date.now(), out = this.events.splice(0);
      // GPS가 아직 없어도(위치 없음) 하트비트가 오면 보냄 → 앱이 '연결됨 · GPS 대기'로 표시
      Object.keys(this.veh).forEach(sid => { const v = this.veh[sid]; if (now - v.seen < STALE_MS) out.push(Object.assign({}, v.tel)); });
      this._emit(out);
    }
    _v(sid) { return this.veh[sid] || (this.veh[sid] = { comp: 1, autopilot: null, seen: 0, tel: { type: "telemetry", id: sid } }); }

    _data(u8) {
      const msgs = [];
      this.ps.push(u8, msgs);
      for (const m of msgs) { this._handle(m); this.waiters.slice().forEach(w => { if (w.match(m)) w.done(m); }); }
    }
    _handle(m) {
      const sid = m.sys, v = m.v;
      if (sid < 1 || sid > 250) return;
      if (m.id === 0) {   // HEARTBEAT: 비행 제어기 것만 (지상국·카메라·짐벌·라즈베리파이 것은 무시)
        const type = m.p[4], ap = m.p[5];
        if (type === MAV_TYPE_GCS || ap === AP_INVALID) return;
        const x = this._v(sid), base = m.p[6], cm = v.getUint32(0, true), main = (cm >> 16) & 0xff, sub = (cm >>> 24) & 0xff;
        x.comp = m.comp; x.autopilot = ap; x.seen = Date.now();
        x.tel.armed = !!(base & 128);
        x.tel.mode = main === 4 ? (AUTO[sub] || "AUTO") : (MAIN[main] || String(main));
        return;
      }
      if (m.id === 33) {   // GLOBAL_POSITION_INT
        const x = this._v(sid); x.seen = Date.now();
        const hdg = v.getUint16(26, true);
        Object.assign(x.tel, { lat: v.getInt32(4, true) / 1e7, lng: v.getInt32(8, true) / 1e7, alt: Math.round(v.getInt32(16, true) / 100) / 10,
          spd: Math.round(Math.hypot(v.getInt16(20, true), v.getInt16(22, true))) / 100, ts: v.getUint32(0, true) / 1000 });
        if (hdg !== 65535) x.tel.hdg = hdg / 100;
        return;
      }
      if (m.id === 1 || m.id === 147) {   // SYS_STATUS / BATTERY_STATUS: 남은 배터리 %, 전압
        const r = m.id === 1 ? v.getInt8(30) : v.getInt8(35), x = this.veh[sid];
        if (!x) return;
        if (r >= 0) x.tel.bat = r;
        const mv = m.id === 1 ? v.getUint16(14, true) : 0;
        if (mv > 0 && mv < 65535) x.tel.volt = Math.round(mv / 10) / 100;
        return;
      }
      if (m.id === 24) {   // GPS_RAW_INT: 위성 수·고정 상태 (안내용)
        if (this.veh[sid]) { this.veh[sid].tel.fix = m.p[28]; this.veh[sid].tel.sats = m.p[29]; }
        return;
      }
      if (m.id === 253) {  // STATUSTEXT: PX4 안내, 그리고 라즈베리파이의 탐지 알림
        let s = ""; for (let i = 1; i < 51 && m.p[i]; i++) s += String.fromCharCode(m.p[i]);
        try { s = decodeURIComponent(escape(s)); } catch (e) { }
        s = s.trim();
        const d = /^DET\s+(\S+)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)/.exec(s);
        if (d) this.events.push({ type: "detection", id: sid, uid: "L" + d[1], lat: +d[2], lng: +d[3], temp: +d[4], alt: this.veh[sid] ? this.veh[sid].tel.alt : null });
        else this.events.push({ type: "status", id: sid, text: s, sev: m.p[0] });
      }
    }
    /* 원하는 메시지가 올 때까지 기다림 (다른 지상국에 간 응답은 무시) */
    _wait(sid, ids, ms, pred) {
      return new Promise(res => {
        const w = {
          match: m => m.sys === sid && ids.includes(m.id) && this._forUs(m) && (!pred || pred(m)),
          done: m => { clearTimeout(w.t); this.waiters = this.waiters.filter(x => x !== w); res(m); }
        };
        w.t = setTimeout(() => w.done(null), ms);
        this.waiters.push(w);
      });
    }
    _forUs(m) {
      const ts = m.id === 77 ? (m.len > 8 ? m.p[8] : 0) : m.id === 47 ? m.p[0] : (m.id === 40 || m.id === 51) ? m.p[2] : 0;
      return ts === 0 || ts === GCS_SYS;
    }
    _ack(op, sid, ok, msg) { this.events.push({ type: "ack", op, id: sid, ok, msg }); this._flush(); }
    _job(sid, fn) {   // 기체별로 차례대로
      const prev = this.queue[sid] || Promise.resolve();
      return this.queue[sid] = prev.then(fn).catch(e => ({ ok: false, msg: "오류: " + (e && e.message || e) }));
    }
    _ready(sid) {
      const x = this.veh[sid];
      if (!x) return "이 번호(SYSID)의 기체 신호가 없습니다";
      if (Date.now() - x.seen > STALE_MS) return `기체 신호가 끊겼습니다 (${Math.round((Date.now() - x.seen) / 1000)}초째 하트비트 없음)`;
      if (x.autopilot !== AP_PX4) return "PX4 기체가 아닙니다 (PX4 전용)";
      return null;
    }

    async _upload(sid, d) {
      const items = buildItems(d), n = items.length, comp = this.veh[sid].comp;
      for (let attempt = 0; attempt < 3; attempt++) {
        this._write(this.pk.missionCount(sid, comp, n));
        const sent = new Set();
        for (;;) {
          const m = await this._wait(sid, [40, 51, 47], 3000);
          if (!m) break;                                    // 응답 없음 → 처음부터 다시
          if (m.id === 47) {
            const t = m.p[2];
            if (t === 0 && sent.size === n) return { ok: true, msg: `${n}개 항목 업로드 완료 (웨이포인트 ${d.route.length}개)` };
            if (t !== 0) return { ok: false, msg: `PX4가 임무를 거부했습니다 (${MISSION_RESULT[t] || t})` };
            continue;
          }
          const seq = m.v.getUint16(0, true);
          if (seq >= n) continue;
          this._write(this.pk.missionItem(sid, comp, seq, items[seq], m.id === 51));   // 요청한 형식 그대로 답함
          sent.add(seq);
        }
      }
      return { ok: false, msg: "기체 응답 없음 (3회 시도) · 무선기 연결과 거리를 확인하세요" };
    }
    async _command(sid, cmd, params) {
      for (let i = 0; i < 2; i++) {   // 무선 구간에서 빠질 수 있어 한 번 더
        this._write(this.pk.commandLong(sid, this.veh[sid].comp, cmd, params));
        const a = await this._wait(sid, [77], 2500, m => m.v.getUint16(0, true) === cmd);
        if (a) return { ok: a.p[2] === 0, r: MAV_RESULT[a.p[2]] || String(a.p[2]) };
      }
      return { ok: false, r: "응답 없음" };
    }
    async _start(sid) {
      let a = await this._command(sid, CMD.ARM, [1]);
      if (!a.ok) return { ok: false, msg: `시동 거부 (${a.r}) · PX4 사전 점검 메시지를 확인하세요 (GPS, 배터리, 안전 스위치, 원격 조종기)` };
      a = await this._command(sid, CMD.SET_MODE, PX4_MODE.MISSION);
      if (!a.ok) { await this._command(sid, CMD.ARM, [0]); return { ok: false, msg: `미션 모드 전환 실패 (${a.r}) · 시동을 껐습니다` }; }
      return { ok: true, msg: "시동 · 미션 모드 · 이륙" };
    }
    async _rtl(sid) {
      const a = await this._command(sid, CMD.SET_MODE, PX4_MODE.RTL);
      return a.ok ? { ok: true, msg: "복귀 모드로 전환" } : { ok: false, msg: `복귀 전환 실패 (${a.r})` };
    }

    /* 앱이 보낸 명령 (브리지와 같은 JSON) */
    send(text) {
      if (this.readyState !== 1) throw new Error("연결되어 있지 않습니다");
      let m; try { m = JSON.parse(text); } catch (e) { return; }
      if (!m || typeof m !== "object") return;
      if (m.type === "upload") {
        (m.drones || []).forEach(d => {
          const sid = d.id | 0, err = checkRoute(d) || this._ready(sid);
          if (err) return this._ack("upload", sid, false, err);
          this._job(sid, () => this._upload(sid, d)).then(r => this._ack("upload", sid, r.ok, r.msg));
        });
      } else if (m.type === "start" || m.type === "rtl") {
        (m.ids || []).forEach(s => {
          const sid = s | 0, err = this._ready(sid);
          if (err) return this._ack(m.type, sid, false, err);
          this._job(sid, () => m.type === "start" ? this._start(sid) : this._rtl(sid)).then(r => this._ack(m.type, sid, r.ok, r.msg));
        });
      }
      // auth 등은 무선기에는 필요 없음
    }
  }
  UsbMavLink.available = () => !!(window.__usbBackend || (window.GCSAndroid && window.GCSAndroid.usbOpen) || navigator.serial);
  UsbMavLink._test = { x25, buildItems, checkRoute, Packer, Parser, EXTRA };
  window.UsbMavLink = UsbMavLink;
})();
