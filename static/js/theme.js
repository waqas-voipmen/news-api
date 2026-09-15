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
    location.reload();
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyIcon(document.documentElement.getAttribute('data-theme') || 'dark');
    document.querySelectorAll('.theme-toggle').forEach(function (btn) {
      btn.addEventListener('click', toggleTheme);
    });
  });
})();
