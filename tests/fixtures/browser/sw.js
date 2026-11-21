self.addEventListener('fetch',event=>event.respondWith(new Response('A service worker must never control a publication.')));
