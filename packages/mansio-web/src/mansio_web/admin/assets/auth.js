import { api } from './api.js';
import { state } from './state.js';
import { hideLoginOverlay, showLoginOverlay } from './util.js';
import { refreshCurrentTab } from './refresh.js';
import { loadDashboard } from './views/dashboard.js';
import { populateChannelSelector } from './views/messages.js';

export async function doLogin() {
    const pw = document.getElementById('loginPassword').value;
    if (!pw) return;
    try {
        const result = await api.login(pw);
        if (result.ok) {
            hideLoginOverlay();
            document.getElementById('logoutBtn').style.display = '';
            await loadDashboard();
            populateChannelSelector();
            state.refreshInterval = setInterval(refreshCurrentTab, 3000);
        } else {
            document.getElementById('loginError').textContent = result.data.error || 'Invalid password';
        }
    } catch (e) {
        document.getElementById('loginError').textContent = 'Connection error';
    }
}

export async function doLogout() {
    await api.logout();
    document.getElementById('logoutBtn').style.display = 'none';
    showLoginOverlay();
}
