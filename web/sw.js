// sw.js — service worker minimo per rendere l'app installabile.
// Cache-first solo per lo shell statico (HTML/manifest/icone), MAI per le
// chiamate /api/* — i dati del portafoglio devono sempre essere freschi,
// mai serviti dalla cache offline (mostrerebbero un numero vecchio come se
// fosse reale, esattamente il tipo di bug trovato oggi su /income).
const CACHE = "the-machine-shell-v1";
const SHELL = [
  "/app/",
  "/app/index.html",
  "/app/manifest.json",
  "/app/icon-192.png",
  "/app/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return; // mai in cache
  if (event.request.method !== "GET") return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((resp) => {
          if (resp.ok) caches.open(CACHE).then((c) => c.put(event.request, resp.clone()));
          return resp;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
