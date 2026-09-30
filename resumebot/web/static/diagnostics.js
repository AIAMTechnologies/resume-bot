(() => {
  let reported = 0;
  async function report(message, stack = '', kind = 'javascript') {
    if (reported++ >= 20) return;
    try {
      await fetch('/api/browser-errors', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: String(message).slice(0, 4000), stack: String(stack).slice(0, 12000), page: location.pathname, kind})
      });
    } catch (_) { /* Avoid recursive errors if the server is unavailable. */ }
    const banner = document.getElementById('dashboard-error-banner');
    if (banner) banner.hidden = false;
  }
  window.addEventListener('error', event => {
    if (event instanceof ErrorEvent) report(event.message, event.error?.stack || '');
    else if (event.target?.tagName === 'SCRIPT' || event.target?.tagName === 'LINK') {
      const url = new URL(event.target.src || event.target.href, location.href);
      report(`Failed to load ${url.origin}${url.pathname}`, '', 'resource');
    }
  }, true);
  window.addEventListener('unhandledrejection', event => report(event.reason?.message || event.reason, event.reason?.stack || '', 'promise'));
  document.addEventListener('htmx:responseError', event => report(`Request returned HTTP ${event.detail.xhr.status}`, '', 'htmx'));
  document.addEventListener('htmx:sendError', () => report('Could not reach the dashboard server', '', 'network'));
})();
