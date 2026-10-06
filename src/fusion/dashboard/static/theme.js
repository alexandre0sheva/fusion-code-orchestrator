// Runs before first paint: apply the viewer's saved theme (if any) so there is no flash.
(function () {
  try {
    var saved = localStorage.getItem('fusion-theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved);
  } catch (e) { /* storage unavailable: follow the system setting */ }
})();
