/*
 * firebase-link.js — 드론 수색 GCS 앱 ⇄ Firebase Realtime Database
 *
 * 기존 앱의 WebSocket 브리지 연결과 똑같이 쓰도록 만든 연결 객체입니다.
 *   const ws = new FirebaseLink(FIREBASE_CONFIG);   // new WebSocket(url) 대신
 *   ws.onopen / ws.onmessage(e → e.data는 JSON 배열) / ws.onclose / ws.send(JSON 문자열) / ws.close()
 * 받는 메시지와 보내는 메시지 형식은 gcs_bridge.py와 같습니다.
 *   받음 {"type":"telemetry","id":1,"lat":..,"lng":..,"alt":..,"bat":..,"hdg":..,"spd":..,"mode":"MISSION","armed":true,...}
 *        {"type":"ack","op":"upload|start|rtl","id":1,"ok":true,"msg":"..."}
 *        {"type":"log","id":1,"text":"..."}            (PX4 상태 메시지)
 *   보냄 {"type":"upload","mission":"M..","drones":[{"id":1,"home":[lat,lng],"alt":100,"speed":8,"shot":39.1,
 *                  "takeoff":120.5,"cam":[1,40],"route":[[lat,lng,출발지 대비 고도m],..]}]}
 *        (점의 고도·takeoff·cam이 없으면 예전 방식: 모든 점 alt, 처음부터 끝까지 촬영)
 *        {"type":"start","ids":[1,2]}   {"type":"rtl","ids":[1,2]}
 *
 * 로그인과 기록 조회는 FirebaseGCS 를 씁니다.
 *   await FirebaseGCS.signIn(cfg, email, password)    // 한 번 로그인하면 기기에 저장되어 자동 로그인
 *   FirebaseGCS.user(cfg)  → {uid, email} 또는 null
 *   FirebaseGCS.signOut(cfg)
 *   await FirebaseGCS.listMissions(cfg, 20)           // 최근 임무 목록 (최신순)
 *   await FirebaseGCS.loadMission(cfg, id)            // 임무 계획 (드론별 경로)
 *   await FirebaseGCS.loadTracks(cfg, id)             // {1:[{t,lat,lng,alt,bat},..], 2:[..]} 실제 비행 궤적
 *   await FirebaseGCS.loadLogs(cfg, droneId, 50)      // PX4 상태 메시지
 *   await FirebaseGCS.deleteMission(cfg, id)          // 임무와 궤적 삭제
 *
 * Firebase SDK 없이 REST API와 EventSource(서버 전송 이벤트)만 씁니다. 외부 스크립트를 받지 않으므로
 * 안드로이드 WebView의 로컬 파일로도 그대로 동작합니다.
 *
 * cfg = { apiKey, databaseURL, authEmulator? }   (에뮬레이터: databaseURL "http://10.0.2.2:9000/?ns=데모", authEmulator "http://10.0.2.2:9099")
 */
(function (global) {
  "use strict";

  const SV = { ".sv": "timestamp" };
  const FRESH_MS = 15000;          // 이보다 오래된 위치는 '실시간'으로 내보내지 않음
  const ACK_WINDOW_MS = 5000;
  const PRESENCE_MS = 30000;
  const TOKEN_REOPEN_MS = 50 * 60 * 1000;

  const store = {
    key: (cfg) => "gcs.firebase." + (cfg.apiKey || "") + "." + (cfg.databaseURL || ""),
    get(cfg) { try { return JSON.parse(localStorage.getItem(store.key(cfg)) || "null"); } catch (e) { return null; } },
    set(cfg, v) { try { v ? localStorage.setItem(store.key(cfg), JSON.stringify(v)) : localStorage.removeItem(store.key(cfg)); } catch (e) { /* 저장소 없음: 이번 실행 동안만 유지 */ } },
  };

  const FRIENDLY = {
    EMAIL_NOT_FOUND: "등록되지 않은 이메일입니다",
    INVALID_PASSWORD: "비밀번호가 틀렸습니다",
    INVALID_LOGIN_CREDENTIALS: "이메일 또는 비밀번호가 틀렸습니다",
    USER_DISABLED: "사용 중지된 계정입니다",
    TOO_MANY_ATTEMPTS_TRY_LATER: "시도가 너무 많습니다. 잠시 뒤 다시 하세요",
    TOKEN_EXPIRED: "로그인이 만료되었습니다. 다시 로그인하세요",
    INVALID_REFRESH_TOKEN: "로그인이 만료되었습니다. 다시 로그인하세요",
    "Permission denied": "권한이 없습니다 (보안 규칙 · 계정 등록을 확인하세요)",
  };
  const friendly = (m) => {
    const s = String(m);
    if (/^Permission denied/.test(s)) return FRIENDLY["Permission denied"];
    return FRIENDLY[s.split(" ")[0].replace(/:$/, "")] || s;
  };

  // ------------------------------ 인증 ------------------------------
  class Auth {
    constructor(cfg) {
      this.cfg = cfg;
      const base = cfg.authEmulator ? cfg.authEmulator.replace(/\/$/, "") + "/" : "https://";
      this.signUrl = base + "identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + encodeURIComponent(cfg.apiKey);
      this.tokenUrl = base + "securetoken.googleapis.com/v1/token?key=" + encodeURIComponent(cfg.apiKey);
      this.s = store.get(cfg);
      this.pending = null;
    }
    get user() { return this.s && this.s.refreshToken ? { uid: this.s.uid, email: this.s.email } : null; }
    // 인증 서버 요청: 통신 실패·서버 오류를 한국어 메시지로. onFail(r) = 오류 응답일 때 먼저 할 일
    async _post(url, type, body, onFail) {
      let r;
      try { r = await fetch(url, { method: "POST", headers: { "Content-Type": type }, body }); }
      catch (e) { throw new Error("서버에 연결할 수 없습니다 (인터넷 연결을 확인하세요)"); }
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { if (onFail) onFail(r); throw new Error(friendly(j.error && j.error.message || r.status)); }
      return j;
    }
    async signIn(email, password) {
      const j = await this._post(this.signUrl, "application/json", JSON.stringify({ email, password, returnSecureToken: true }));
      this.s = { uid: j.localId, email: j.email || email, refreshToken: j.refreshToken, idToken: j.idToken,
                 exp: Date.now() + (+j.expiresIn || 3600) * 1000 };
      store.set(this.cfg, this.s);
      return this.user;
    }
    signOut() { this.s = null; store.set(this.cfg, null); }
    async token(force) {
      if (!this.s || !this.s.refreshToken) throw new Error("로그인이 필요합니다");
      if (!force && this.s.idToken && Date.now() < this.s.exp - 300000) return this.s.idToken;
      if (this.pending) return this.pending;
      this.pending = (async () => {
        const j = await this._post(this.tokenUrl, "application/x-www-form-urlencoded",
          "grant_type=refresh_token&refresh_token=" + encodeURIComponent(this.s.refreshToken),
          (r) => { if (r.status === 400) this.signOut(); });   // 비밀번호 변경·계정 삭제 등 → 다시 로그인
        Object.assign(this.s, { idToken: j.id_token, refreshToken: j.refresh_token, exp: Date.now() + (+j.expires_in || 3600) * 1000 });
        store.set(this.cfg, this.s);
        return this.s.idToken;
      })();
      try { return await this.pending; } finally { this.pending = null; }
    }
  }

  // ------------------------------ 데이터베이스 ------------------------------
  class Db {
    constructor(cfg, auth) {
      const u = new URL(cfg.databaseURL);
      this.origin = u.origin;
      this.base = u.pathname.replace(/\/$/, "");
      this.ns = u.searchParams.get("ns");
      this.auth = auth;
    }
    async url(path, params) {
      const q = new URLSearchParams();
      for (const [k, v] of Object.entries(params || {})) q.set(k, ["orderBy", "startAt", "endAt", "equalTo"].includes(k) ? JSON.stringify(v) : v);
      q.set("auth", await this.auth.token());
      if (this.ns) q.set("ns", this.ns);
      return `${this.origin}${this.base}/${String(path).replace(/^\/|\/$/g, "")}.json?${q}`;
    }
    async req(method, path, body, params) {
      for (let attempt = 0; attempt < 2; attempt++) {
        let r;
        try {
          r = await fetch(await this.url(path, params), { method, body: body === undefined ? undefined : JSON.stringify(body),
            headers: body === undefined ? undefined : { "Content-Type": "application/json" } });
        } catch (e) { throw new Error("서버에 연결할 수 없습니다 (인터넷 연결을 확인하세요)"); }
        if (r.status === 401 && attempt === 0) { await this.auth.token(true); continue; }
        const text = await r.text();
        let j = null;
        try { j = text ? JSON.parse(text) : null; } catch (e) { j = text; }
        if (!r.ok) throw new Error(friendly(j && j.error || r.status));
        return j;
      }
    }
    get(path, params) { return this.req("GET", path, undefined, params); }
    put(path, body) { return this.req("PUT", path, body); }
    post(path, body) { return this.req("POST", path, body); }
    del(path) { return this.req("DELETE", path); }

    /** 실시간 구독. handlers: {put(path, data), patch(path, data), open(), error(err)} → {close()} */
    stream(path, handlers) {
      let es = null, closed = false, retry = 1000, timer = null;
      const open = async () => {
        if (closed) return;
        let url;
        try { url = await this.url(path); } catch (e) { handlers.error && handlers.error(e, true); return; }
        if (closed) return;
        es = new EventSource(url);
        const on = (name, fn) => es.addEventListener(name, (ev) => { let d = null; try { d = JSON.parse(ev.data); } catch (e) { /* keep-alive */ } fn(d); });
        on("put", (d) => { retry = 1000; d && handlers.put && handlers.put(d.path, d.data); });
        on("patch", (d) => { d && handlers.patch && handlers.patch(d.path, d.data); });
        const reauth = async () => { es.close(); try { await this.auth.token(true); } catch (e) { handlers.error && handlers.error(e, true); return; } open(); };
        es.addEventListener("auth_revoked", reauth);
        es.addEventListener("cancel", () => { es.close(); handlers.error && handlers.error(new Error(friendly("Permission denied")), true); });
        es.onerror = () => {
          if (closed) return;
          if (es.readyState === EventSource.CLOSED) {   // 브라우저가 포기한 경우 직접 다시 연결
            handlers.error && handlers.error(new Error("연결 끊김"), false);
            clearTimeout(timer);
            timer = setTimeout(open, retry);
            retry = Math.min(retry * 2, 30000);
          }
        };
      };
      open();
      return {
        close() { closed = true; clearTimeout(timer); if (es) es.close(); },
        reopen() { if (es) es.close(); open(); },
      };
    }
  }

  // 같은 설정이면 인증·DB 객체를 공유
  const pool = new Map();
  function client(cfg) {
    if (!cfg || !cfg.apiKey || !cfg.databaseURL) throw new Error("Firebase 설정(apiKey, databaseURL)이 비어 있습니다");
    const k = store.key(cfg) + "|" + (cfg.authEmulator || "");
    if (!pool.has(k)) { const auth = new Auth(cfg); pool.set(k, { auth, db: new Db(cfg, auth) }); }
    return pool.get(k);
  }

  function setAt(tree, path, val) {
    const parts = String(path).split("/").filter(Boolean);
    if (!parts.length) return val && typeof val === "object" ? val : {};
    let o = tree;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!o[parts[i]] || typeof o[parts[i]] !== "object") o[parts[i]] = {};
      o = o[parts[i]];
    }
    const last = parts[parts.length - 1];
    if (val === null || val === undefined) delete o[last]; else o[last] = val;
    return tree;
  }
  const safeId = (s) => String(s || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40);
  const asList = (v) => Array.isArray(v) ? v : v && typeof v === "object" ? Object.keys(v).sort((a, b) => a - b).map((k) => v[k]) : [];

  // ------------------------------ WebSocket 호환 연결 ------------------------------
  class FirebaseLink {
    constructor(cfg) {
      this.cfg = cfg;
      this.url = "firebase:" + (cfg && cfg.databaseURL);
      this.readyState = FirebaseLink.CONNECTING;
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this.offset = 0;
      this.live = {};
      this.mission = null;
      this.timers = [];
      this.streams = [];
      this.seenAcks = new Set();
      this.seenLogs = new Set();
      setTimeout(() => this._open(), 0);
    }
    serverNow() { return Date.now() + this.offset; }

    async _open() {
      try {
        const { auth, db } = client(this.cfg);
        this.auth = auth; this.db = db;
        if (!auth.user) throw new Error("로그인이 필요합니다 (설정 → Firebase 로그인)");
        const uid = auth.user.uid;
        const t0 = Date.now();
        const r = await db.put(`presence/${uid}`, { at: SV });
        this.offset = r.at - (t0 + Date.now()) / 2;
        const isOp = await db.get(`config/operators/${uid}`);
        if (isOp !== true) throw new Error(`운영자로 등록되지 않은 계정입니다. Firebase 콘솔에서 /config/operators/${uid} = true 로 설정하세요`);
        if (this.readyState === FirebaseLink.CLOSED) return;
        this.since = this.serverNow() - ACK_WINDOW_MS;

        this.streams.push(db.stream("live", {
          put: (p, d) => { this.live = setAt(this.live, p, d); this._ready(); this._emitLive(p, d); },
          patch: (p, d) => { for (const k in d) this.live = setAt(this.live, p.replace(/\/$/, "") + "/" + k, d[k]); this._emitLive(p, d); },
          error: (e, fatal) => fatal ? this._fail(e) : this._error(e),
        }));
        // 응답(acks)·PX4 메시지(logs): 같은 방식으로 모음. 로그 스트림 오류는 연결을 끊지 않음
        const collect = (path, kind, error) => this.streams.push(db.stream(path, {
          put: (p, d) => this._collect(p, d, kind),
          patch: (p, d) => { for (const k in d) this._collect(p.replace(/\/$/, "") + "/" + k, d[k], kind); },
          error,
        }));
        collect("acks", "ack", (e, fatal) => fatal ? this._fail(e) : this._error(e));
        collect("logs", "log", () => {});
        this.timers.push(setInterval(() => db.put(`presence/${uid}`, { at: SV }).catch(() => {}), PRESENCE_MS));
        this.timers.push(setInterval(() => this.streams.forEach((s) => s.reopen()), TOKEN_REOPEN_MS));
      } catch (e) { this._fail(e); }
    }

    _ready() {
      if (this.readyState !== FirebaseLink.CONNECTING) return;
      this.readyState = FirebaseLink.OPEN;
      this.onopen && this.onopen({ type: "open", target: this });
    }
    _emit(msgs) {
      if (!msgs.length || this.readyState !== FirebaseLink.OPEN) return;
      this.onmessage && this.onmessage({ type: "message", data: JSON.stringify(msgs), target: this });
    }
    _error(e) { this.onerror && this.onerror({ type: "error", message: e.message, target: this }); }
    _fail(e) {
      if (this.readyState === FirebaseLink.CLOSED) return;
      this.lastError = e && e.message || String(e);
      console.warn("[FirebaseLink]", this.lastError);
      this._error(e);
      this._shutdown();
      this.onclose && this.onclose({ type: "close", code: 4000, reason: this.lastError, wasClean: false, target: this });
    }
    _shutdown() {
      this.readyState = FirebaseLink.CLOSED;
      this.streams.forEach((s) => s.close());
      this.timers.forEach(clearInterval);
      this.streams = []; this.timers = [];
    }
    close() {
      if (this.readyState === FirebaseLink.CLOSED) return;
      this._shutdown();
      this.onclose && this.onclose({ type: "close", code: 1000, reason: "", wasClean: true, target: this });
    }

    _emitLive(path, data) {
      const first = String(path).split("/").filter(Boolean)[0];
      const ids = first ? [first] : Object.keys(data || {});
      const now = this.serverNow(), out = [];
      for (const id of ids) {
        const L = this.live[id];
        if (!L || typeof L !== "object" || !(now - (+L.at || 0) < FRESH_MS)) continue;   // 꺼진 드론의 마지막 위치는 건너뜀
        out.push(Object.assign({ type: "telemetry", id: +id }, L));
      }
      this._emit(out);
    }
    _collect(path, data, type) {
      // /acks 와 /logs 는 {드론id: {키: 값}} 구조. 연결한 뒤에 생긴 것만 내보낸다.
      const parts = String(path).split("/").filter(Boolean);
      let rows = [];
      if (parts.length === 0) for (const id in data || {}) for (const k in data[id] || {}) rows.push([id, k, data[id][k]]);
      else if (parts.length === 1) for (const k in data || {}) rows.push([parts[0], k, data[k]]);
      else rows.push([parts[0], parts[1], data]);
      const seen = type === "ack" ? this.seenAcks : this.seenLogs, out = [];
      for (const [id, k, v] of rows) {
        if (!v || typeof v !== "object" || seen.has(k) || !(+v.at >= this.since)) continue;
        seen.add(k);
        out.push(type === "ack" ? { type: "ack", op: v.op, id: +id, ok: !!v.ok, msg: v.msg || "", mission: v.mission || null }
                                : { type: "log", id: +id, text: v.text || "" });
      }
      this._emit(out);
    }

    /** 기존 브리지와 같은 JSON 문자열(또는 객체)을 받는다 */
    send(data) {
      if (this.readyState !== FirebaseLink.OPEN) throw new Error("연결되지 않았습니다");
      let m;
      try { m = typeof data === "string" ? JSON.parse(data) : data; } catch (e) { return; }
      this._send(m).catch((e) => {
        const ids = m.type === "upload" ? asList(m.drones).map((d) => d.id) : asList(m.ids);
        const fail = ids.map((id) => ({ type: "ack", op: m.type, id: +id, ok: false, msg: "서버 전송 실패: " + e.message }));
        this._emit(fail);
      });
    }
    async _send(m) {
      const uid = this.auth.user.uid, db = this.db;
      if (m.type === "upload") {
        const mission = safeId(m.mission) || "M" + Date.now();
        const drones = {};
        for (const d of asList(m.drones)) {
          const id = Math.round(+d.id);
          if (!(id >= 1 && id <= 8)) continue;
          // 점마다 출발지 대비 고도(3번째 값)가 있으면 그대로 보냄 (지형 따라 고도). takeoff·cam도 앱이 준 그대로
          drones[id] = { home: [+d.home[0], +d.home[1]], alt: +d.alt, speed: +d.speed, shot: +(d.shot || 0),
                         route: asList(d.route).map((p) => p.length > 2 && isFinite(+p[2]) ? [+(+p[0]).toFixed(7), +(+p[1]).toFixed(7), +(+p[2]).toFixed(1)] : [+(+p[0]).toFixed(7), +(+p[1]).toFixed(7)]) };
          if (d.takeoff != null && isFinite(+d.takeoff)) drones[id].takeoff = +(+d.takeoff).toFixed(1);
          if (Array.isArray(d.cam) && d.cam.length === 2) drones[id].cam = [Math.round(+d.cam[0]), Math.round(+d.cam[1])];
        }
        if (!Object.keys(drones).length) throw new Error("업로드할 드론이 없습니다");
        const body = { created: SV, by: uid, drones };
        if (m.name) body.name = String(m.name).slice(0, 80);
        if (m.area != null && isFinite(+m.area)) body.area = +m.area;
        await db.put(`missions/${mission}`, body);
        this.mission = mission;
        await Promise.all(Object.keys(drones).map((id) => db.post(`commands/${id}`, { type: "upload", mission, at: SV, by: uid })));
        return;
      }
      if (m.type === "start" || m.type === "rtl") {
        const mission = safeId(m.mission) || this.mission;
        await Promise.all(asList(m.ids).map((id) => {
          const c = { type: m.type, at: SV, by: uid };
          if (mission && m.type === "start") c.mission = mission;
          return db.post(`commands/${Math.round(+id)}`, c);
        }));
        return;
      }
      throw new Error("알 수 없는 메시지: " + m.type);
    }
  }
  FirebaseLink.CONNECTING = 0; FirebaseLink.OPEN = 1; FirebaseLink.CLOSING = 2; FirebaseLink.CLOSED = 3;

  // ------------------------------ 로그인 · 기록 조회 ------------------------------
  const FirebaseGCS = {
    Link: FirebaseLink,
    user: (cfg) => { try { return client(cfg).auth.user; } catch (e) { return null; } },
    signIn: (cfg, email, password) => client(cfg).auth.signIn(email, password),
    signOut: (cfg) => client(cfg).auth.signOut(),

    async listMissions(cfg, limit = 20) {
      const all = await client(cfg).db.get("missions", { orderBy: "created", limitToLast: limit });
      return Object.entries(all || {}).map(([id, m]) => ({
        id, created: m.created, by: m.by, name: m.name || "", area: m.area ?? null,
        drones: Object.keys(m.drones || {}).map(Number).sort((a, b) => a - b),
        points: Object.values(m.drones || {}).reduce((n, d) => n + asList(d.route).length, 0),
      })).sort((a, b) => b.created - a.created);
    },
    async loadMission(cfg, id) {
      const m = await client(cfg).db.get(`missions/${safeId(id)}`);
      if (!m) return null;
      for (const k in m.drones || {}) m.drones[k].route = asList(m.drones[k].route);
      return Object.assign({ id }, m);
    },
    async loadTracks(cfg, id) {
      const t = await client(cfg).db.get(`tracks/${safeId(id)}`) || {};
      const out = {};
      for (const droneId in t) {
        out[droneId] = Object.keys(t[droneId] || {}).sort().map((k) => {
          const p = asList(t[droneId][k]);
          return { t: +k, lat: +p[0], lng: +p[1], alt: p[2] == null ? null : +p[2], bat: p[3] == null ? null : +p[3] };
        });
      }
      return out;
    },
    async loadLogs(cfg, droneId, limit = 50) {
      const l = await client(cfg).db.get(`logs/${Math.round(+droneId)}`, { orderBy: "$key", limitToLast: limit }) || {};
      return Object.keys(l).sort().map((k) => ({ at: l[k].at, text: l[k].text }));
    },
    async deleteMission(cfg, id) {
      const db = client(cfg).db;
      await db.del(`tracks/${safeId(id)}`);
      await db.del(`missions/${safeId(id)}`);
    },
    trackLength(points) {
      let d = 0;
      for (let i = 1; i < points.length; i++) {
        const a = points[i - 1], b = points[i], r = Math.PI / 180;
        const h = Math.sin((b.lat - a.lat) * r / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lng - a.lng) * r / 2) ** 2;
        d += 12742000 * Math.asin(Math.min(1, Math.sqrt(h)));
      }
      return d;
    },
  };

  global.FirebaseLink = FirebaseLink;
  global.FirebaseGCS = FirebaseGCS;
})(typeof window !== "undefined" ? window : globalThis);
