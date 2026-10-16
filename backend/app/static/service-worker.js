/**
 * service-worker.js
 * Service Worker com Estratégia Híbrida de Cache para PWA AcadAgent:
 * - App Shell e Ativos Estáticos: Stale-While-Revalidate
 * - Leitura da API (GET /api/dashboard/daily): Network-First com Fallback Offline
 * - Mutações (POST /api/...): Pass-Through Direto
 */

const CACHE_NAME = 'acadagent-shell-v1';

const APP_SHELL_ASSETS = [
  '/',
  '/static/index.html',
  '/static/app.js',
  '/manifest.json',
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Ciclo de Instalação: Pré-cache do App Shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        return cache.addAll(APP_SHELL_ASSETS);
      })
      .then(() => self.skipWaiting())
  );
});

// Ciclo de Ativação: Limpeza de caches obsoletos e controle imediato
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((cacheNames) => {
        return Promise.all(
          cacheNames
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name))
        );
      })
      .then(() => self.clients.claim())
  );
});

// Ciclo de Interceptação de Requisições (Fetch)
self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Mutações (POST, PUT, DELETE, etc.) e métodos não-GET: Pass-Through direto para a rede
  if (request.method !== 'GET') {
    return;
  }

  const url = new URL(request.url);

  // 1. Endpoint de Dados: GET /api/dashboard/daily -> Network-First com Fallback para Cache
  if (url.pathname === '/api/dashboard/daily') {
    event.respondWith(
      fetch(request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const responseClone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(request, responseClone);
            });
          }
          return networkResponse;
        })
        .catch(() => {
          return caches.match(request).then((cachedResponse) => {
            if (cachedResponse) {
              return cachedResponse;
            }
            // Fallback JSON estruturado caso nunca tenha havido cache
            return new Response(
              JSON.stringify({
                date: new Date().toISOString().split('T')[0],
                classes_and_allocations: [],
                reading_queue: [],
                domestic_routines: [],
                offline_fallback: true
              }),
              {
                headers: { 'Content-Type': 'application/json' },
                status: 200
              }
            );
          });
        })
    );
    return;
  }

  // 2. Outras rotas de API: Pass-Through de rede
  if (url.pathname.startsWith('/api/')) {
    return;
  }

  // 3. Ativos Estáticos e Navegação -> Stale-While-Revalidate
  event.respondWith(
    caches.match(request).then((cachedResponse) => {
      const fetchPromise = fetch(request)
        .then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const responseClone = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(request, responseClone);
            });
          }
          return networkResponse;
        })
        .catch(() => {
          // Se for requisição de navegação e falhar a rede, entrega a raiz em cache
          if (request.mode === 'navigate') {
            return caches.match('/');
          }
          return cachedResponse;
        });

      return cachedResponse || fetchPromise;
    })
  );
});
