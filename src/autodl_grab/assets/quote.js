// Observe the page's own price previews. No requests are created or replayed.
(() => {
  if (window.__autodlQuote) return;
  let serial = 0, current = {generation: 0, pending: true};
  const route = url => {
    try {
      const u = new URL(url, location.href);
      return u.origin === 'https://www.autodl.com' && /^\/api\/v1\/(?:sub_user\/)?order\/price\/preview$/.test(u.pathname);
    } catch (_) { return false; }
  };
  const begin = body => {
    const generation = ++serial;
    try { body = typeof body === 'string' ? JSON.parse(body) : null; } catch (_) { body = null; }
    current = {generation, pending: true, request: body};
    return generation;
  };
  const finish = (generation, status, body) => {
    if (generation !== serial) return;
    let payload = null;
    try { payload = JSON.parse(body); } catch (_) {}
    current = {...current, pending: false, status, payload};
  };
  Object.defineProperty(window, '__autodlQuote', {value: Object.freeze({
    read: () => JSON.parse(JSON.stringify(current))
  })});
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  const requests = new WeakMap();
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    requests.set(this, String(method).toUpperCase() === 'POST' && route(url));
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(body) {
    if (requests.get(this)) {
      const generation = begin(body);
      this.addEventListener('loadend', () => {
        let text = '';
        try { text = this.responseType === 'json' ? JSON.stringify(this.response) : this.responseText; } catch (_) {}
        finish(generation, this.status, text);
      }, {once: true});
    }
    return send.call(this, body);
  };
  const fetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const method = String(init?.method || input?.method || 'GET').toUpperCase();
    if (method !== 'POST' || !route(url)) return fetch.apply(this, arguments);
    // Request objects are cloned; reading their body must not consume the page's request.
    const body = init?.body !== undefined ? init.body : input instanceof Request ? await input.clone().text() : null;
    const generation = begin(body);
    try {
      const response = await fetch.apply(this, arguments);
      response.clone().text().then(text => finish(generation, response.status, text), () => finish(generation, 0, ''));
      return response;
    } catch (error) { finish(generation, 0, ''); throw error; }
  };
})();
