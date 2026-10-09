/* 드론 수색 GCS 서비스워커
   - 앱 화면(index.html 등): 네트워크 우선 → 실패하면 캐시 (수정본이 바로 반영되고, 오프라인에서도 열림)
   - 지도 타일: 캐시 우선 → 한 번 본 지역은 현장에서 통신이 끊겨도 보임 (최대 3000장) */
const APP_CACHE = "gcs-app-v33";
const TILE_CACHE = "gcs-tiles-v1";
// "./"(폴더 주소)는 넣지 않음: 안드로이드 앱 안에서는 폴더 주소에 파일이 없어 설치 전체가 실패함
const APP_FILES = ["./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];
const TILE_HOSTS = ["server.arcgisonline.com", "tile.openstreetmap.org"];
const TILE_LIMIT = 3000;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(APP_CACHE).then(c => c.addAll(APP_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== APP_CACHE && k !== TILE_CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

let added = 0;
async function trimTiles() {               // 새 타일 100장마다 한 번만 정리 (매번 목록을 읽으면 느림)
  if (++added % 100) return;
  const c = await caches.open(TILE_CACHE);
  const keys = await c.keys();
  for (let i = 0; i < keys.length - TILE_LIMIT; i++) await c.delete(keys[i]);
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (TILE_HOSTS.includes(url.hostname)) {
    e.respondWith(
      caches.open(TILE_CACHE).then(async c => {
        const hit = await c.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) { c.put(req, res.clone()).then(trimTiles).catch(() => {}); }   // 저장 공간 부족 등은 무시
        return res;
      })
    );
    return;
  }

  if (url.origin === self.location.origin) {
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res.ok) { const copy = res.clone(); caches.open(APP_CACHE).then(c => c.put(req, copy)); }  // 화면에 넘기기 전에 복사
          return res;
        })
        .catch(() => caches.match(req).then(r => r || (req.mode === "navigate" ? caches.match("./index.html") : Response.error())))
    );
  }
});
