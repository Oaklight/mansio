import { state } from './state.js';

// Sender color map
const senderColors = {};

const colorPalette = [
    '#2B8A7E','#C2922E','#7C3AED','#DB2777','#2563EB',
    '#D97706','#059669','#DC2626','#7C3AED','#9333EA',
];

export function getSenderColor(sender) {
    if (!senderColors[sender]) {
        const idx = Object.keys(senderColors).length % colorPalette.length;
        senderColors[sender] = colorPalette[idx];
    }
    return senderColors[sender];
}

/* ============== Status ============== */
export function updateStatus(connected) {
    const dot = document.getElementById('statusDot');
    const label = document.getElementById('statusLabel');
    if (connected) {
        dot.classList.add('connected');
        label.textContent = 'Connected';
    } else {
        dot.classList.remove('connected');
        label.textContent = 'Disconnected';
    }
}

/* ============== Toast ============== */
export function showToast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = 'slideOut 0.3s ease forwards';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

/* ============== Utilities ============== */
export function esc(str) {
    if (str === null || str === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

export function formatTime(isoStr) {
    if (!isoStr) return '-';
    try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch(e) { return isoStr; }
}

/* ============== Login / Logout ============== */
export function showLoginOverlay() {
    document.body.classList.add('auth-pending');
    document.getElementById('loginOverlay').classList.remove('hidden');
    document.getElementById('loginError').textContent = '';
    document.getElementById('loginPassword').value = '';
    document.getElementById('loginPassword').focus();
    if (state.refreshInterval) { clearInterval(state.refreshInterval); state.refreshInterval = null; }
}

export function hideLoginOverlay() {
    document.getElementById('loginOverlay').classList.add('hidden');
    document.body.classList.remove('auth-pending');
}

/* ============== Theme & Settings ============== */
export function toggleSettings() {
    document.getElementById('settingsPopup').classList.toggle('hidden');
    updateThemeSwitcher();
}

export function updateThemeSwitcher() {
    var current = localStorage.getItem('mansio-theme') || 'auto';
    var btns = document.querySelectorAll('#themeSwitcher button');
    var themes = ['light', 'auto', 'dark'];
    btns.forEach(function(btn, i) {
        btn.classList.toggle('active', themes[i] === current);
    });
}

export function isDarkMode() {
    var theme = document.documentElement.dataset.theme;
    if (theme === 'dark') return true;
    if (theme === 'light') return false;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

export function getChartColors() {
    if (isDarkMode()) {
        return {
            grid: '#2a2a4a',
            label: '#8b8b8b',
            line: '#3dd6c8',
            fill: 'rgba(61, 214, 200, 0.08)'
        };
    }
    return {
        grid: '#E8E5E0',
        label: '#81807C',
        line: '#2B8A7E',
        fill: 'rgba(43, 138, 126, 0.08)'
    };
}

export function formatUptime(seconds) {
    if (seconds === undefined || seconds === null) return '\u2014';
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
}
