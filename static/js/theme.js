(function () {
  function applyIcon(theme) {
    document.querySelectorAll('.theme-toggle').forEach(function (btn) {
      btn.textContent = theme === 'dark' ? '☀' : '☽';
      btn.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to night mode';
    });
  }

  function setTheme(theme) {
    document.cookie = 'theme=' + theme + '; path=/; max-age=31536000; SameSite=Lax';
    document.documentElement.setAttribute('data-theme', theme);
    applyIcon(theme);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    setTheme(current === 'dark' ? 'light' : 'dark');
    // Every other page re-themes instantly through pure CSS variables --
    // no reload needed. TradingView's embedded widgets (Live Prices) bake
    // their color theme into the widget's own <script> config at render
    // time though, so those can only pick up the new theme via a fresh
    // page load; reload only when such a widget is actually on the page,
    // instead of unconditionally forcing every page (including News/Market
    // Bias/Social Sentiment, which now show a several-second loading state
    // on every fresh load) through a reload it doesn't need.
    if (document.querySelector('.tradingview-widget-container')) {
      location.reload();
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyIcon(document.documentElement.getAttribute('data-theme') || 'dark');
    document.querySelectorAll('.theme-toggle').forEach(function (btn) {
      btn.addEventListener('click', toggleTheme);
    });
  });
})();
