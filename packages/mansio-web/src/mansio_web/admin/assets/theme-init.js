// Theme initialization — runs before body to prevent flash
(function() {
    var theme = localStorage.getItem('mansio-theme') || 'auto';
    if (theme === 'dark') {
        document.documentElement.dataset.theme = 'dark';
    } else if (theme === 'light') {
        document.documentElement.dataset.theme = 'light';
    } else {
        // auto: let CSS prefers-color-scheme handle it, but don't set data-theme
        // so the @media query applies
        document.documentElement.removeAttribute('data-theme');
    }
})();
