// Service Worker for Stock Sub Dashboard
// Caches static assets for offline use and faster repeat loads

const CACHE_NAME = 'stock-sub-dashboard-v1';
const STATIC_ASSETS = [
  '/',
  '/manifest.json',
];

// Cache static assets on install
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// Clean up old caches on activate
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

// Network-first for API calls, cache-first for static assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  
  // Skip non-GET requests
  if (event.request.method !== 'GET') {
    return;
  }
  
  // Skip cross-origin requests
  if (url.origin !== location.origin) {
    return;
  }
  
  // API calls: network-first, fallback to cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(event.request));
    return;
  }
  
  // Static assets: cache-first
  event.respondWith(cacheFirst(event.request));
});

// Network-first strategy: try network, fallback to cache
async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  
  try {
    const response = await fetch(request);
    
    // Cache successful responses
    if (response.ok) {
      cache.put(request, response.clone());
    }
    
    return response;
  } catch (error) {
    // Network failed, try cache
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    
    // Return offline page for navigation requests
    if (request.mode === 'navigate') {
      return new Response(
        `<!DOCTYPE html>
        <html><head><title>Offline</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body{font-family:system-ui;padding:2rem;text-align:center;background:#0d1117;color:#e6edf3}
        h1{color:#58a6ff}</style></head>
        <body><h1>📈 Offline</h1><p>No internet connection. Cached data may be stale.</p>
        <button onclick="location.reload()">Retry</button></body></html>`,
        { headers: { 'Content-Type': 'text/html' } }
      );
    }
    
    throw error;
  }
}

// Cache-first strategy: try cache, fallback to network
async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  
  if (cached) {
    // Serve from cache, update in background
    fetch(request).then((response) => {
      if (response.ok) cache.put(request, response);
    }).catch(() => {});
    return cached;
  }
  
  // Not in cache, fetch from network
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    // Return offline page for navigation requests
    if (request.mode === 'navigate') {
      return new Response(
        `<!DOCTYPE html>
        <html><head><title>Offline</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>body{font-family:system-ui;padding:2rem;text-align:center;background:#0d1117;color:#e6edf3}
        h1{color:#58a6ff}</style></head>
        <body><h1>📈 Offline</h1><p>No internet connection. Cached data may be stale.</p>
        <button onclick="location.reload()">Retry</button></body></html>`,
        { headers: { 'Content-Type': 'text/html' } }
      );
    }
    throw error;
  }
}