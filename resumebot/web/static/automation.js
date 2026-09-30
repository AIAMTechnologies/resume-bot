(() => {
  const panel = document.getElementById('automation-live');
  if (!panel) return;
  async function refresh() {
    if (!document.hidden) {
      try {
        const response = await fetch('/partials/automation');
        if (!response.ok) throw new Error(`Automation status returned HTTP ${response.status}`);
        panel.innerHTML = await response.text();
      } catch (error) {
        document.getElementById('automation-connection').textContent = 'Status updates interrupted. Displayed information may be stale. Refresh the page or check the error log.';
        setTimeout(refresh, 5000);
        return;
      }
      document.getElementById('automation-connection').textContent = '';
    }
    setTimeout(refresh, 5000);
  }
  setTimeout(refresh, 5000);
})();
