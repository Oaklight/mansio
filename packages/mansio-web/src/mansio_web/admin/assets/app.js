import { api } from './api.js';
import { state } from './state.js';
import { refreshCurrentTab } from './refresh.js';
import { doLogin, doLogout } from './auth.js';
import { formatUptime, showLoginOverlay, toggleSettings, updateThemeSwitcher } from './util.js';
import { loadDashboard } from './views/dashboard.js';
import { closeChannelDetail, openChannelDetail, renderChannelView, setChannelView, toggleTreeGroup } from './views/channels.js';
import { loadMessages, populateChannelSelector, publishMessage, toggleDetailPayload, togglePayload, togglePublish } from './views/messages.js';
import { copyToken, deleteToken, hideCreateTokenModal, loadTokens, rotateToken, showCreateTokenModal, submitCreateToken, syncSupertokenInput } from './views/tokens.js';

/* ============== Tabs ============== */
export function switchTab(tab) {
    state.currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.textContent.toLowerCase() === tab);
    });
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById('tab-' + tab).classList.add('active');

    if (state.refreshInterval) clearInterval(state.refreshInterval);
    refreshCurrentTab();
    state.refreshInterval = setInterval(refreshCurrentTab, 3000);
}

export function setTheme(theme) {
    localStorage.setItem('mansio-theme', theme);
    if (theme === 'dark') {
        document.documentElement.dataset.theme = 'dark';
    } else if (theme === 'light') {
        document.documentElement.dataset.theme = 'light';
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    updateThemeSwitcher();
    // Redraw chart with updated colors
    if (state.currentTab === 'dashboard') loadDashboard();
}

/* ============== Status Footer ============== */
export async function updateFooter() {
    try {
        var stats = await api.getStats();
        document.getElementById('footerMessages').textContent = 'Messages: ' + stats.total_messages.toLocaleString();
    } catch(e) {}
    try {
        var sys = await api.getSystemInfo();
        var b = sys.backend || {};
        document.getElementById('footerBackend').textContent = 'Backend: ' + (b.type || 'unknown');
        if (sys.uptime_seconds !== undefined) {
            document.getElementById('footerUptime').textContent = 'Uptime: ' + formatUptime(sys.uptime_seconds);
        } else if (sys.uptime !== undefined) {
            document.getElementById('footerUptime').textContent = 'Uptime: ' + sys.uptime;
        }
    } catch(e) {}
}

/* ============== Init ============== */
export async function init() {
    try {
        const auth = await api.checkAuth();
        if (auth.required && !auth.authenticated) {
            showLoginOverlay();
            return;
        }
        if (auth.required) {
            document.getElementById('logoutBtn').style.display = '';
        }
    } catch (e) {
        // No auth configured or server unreachable — proceed without login
    }
    document.body.classList.remove('auth-pending');
    await loadDashboard();
    populateChannelSelector();
    updateFooter();
    state.refreshInterval = setInterval(refreshCurrentTab, 3000);
    // Refresh footer periodically (every 30s)
    setInterval(updateFooter, 30000);
}

/* ============== Inline handler bindings ============== */
/* M4a keeps the markup's onclick/onchange attributes working.  Replacing
   them with listeners is a follow-up. */
Object.assign(window, {
    switchTab,
    refreshCurrentTab,
    setTheme,
    loadMessages,
    setChannelView,
    renderChannelView,
    toggleTreeGroup,
    openChannelDetail,
    closeChannelDetail,
    toggleDetailPayload,
    togglePayload,
    togglePublish,
    publishMessage,
    showCreateTokenModal,
    syncSupertokenInput,
    hideCreateTokenModal,
    submitCreateToken,
    deleteToken,
    rotateToken,
    copyToken,
    toggleSettings,
    doLogin,
    doLogout,
});

// Listen for system theme changes to update chart
if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {
        var theme = localStorage.getItem('mansio-theme') || 'auto';
        if (theme === 'auto' && state.currentTab === 'dashboard') loadDashboard();
    });
}

init();
