// Session login overlay. The hash route is never touched here, so a deep
// link survives the login round-trip.

import { api } from './api.js';
import { state } from './state.js';
import { toast } from './ui.js';

function overlay() {
    return document.getElementById('loginOverlay');
}

export function showLoginOverlay() {
    const node = overlay();
    if (!node) return;
    node.classList.remove('hidden');
    document.body.classList.add('auth-pending');
    const error = document.getElementById('loginError');
    if (error) error.textContent = '';
    const password = document.getElementById('loginPassword');
    if (password) {
        password.value = '';
        password.focus();
    }
}

export function hideLoginOverlay() {
    const node = overlay();
    if (!node) return;
    node.classList.add('hidden');
    document.body.classList.remove('auth-pending');
}

/**
 * Wire the login form.
 * @param {Function} onSuccess called after a successful login
 */
export function initAuthForm(onSuccess) {
    const form = document.getElementById('loginForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const passwordField = document.getElementById('loginPassword');
        const errorField = document.getElementById('loginError');
        const button = document.getElementById('loginButton');
        const password = passwordField ? passwordField.value : '';
        if (!password) return;
        if (button) button.disabled = true;
        try {
            const result = await api.login(password);
            if (!result.ok) {
                if (errorField) errorField.textContent = result.data.error || 'Invalid password';
                return;
            }
            state.auth.authenticated = true;
            hideLoginOverlay();
            onSuccess();
        } catch {
            if (errorField) errorField.textContent = 'Connection error';
        } finally {
            if (button) button.disabled = false;
        }
    });
}

/** @param {Function} onLoggedOut called after the session ends */
export async function logout(onLoggedOut) {
    try {
        await api.logout();
    } catch (error) {
        toast('Logout failed: ' + error.message, 'error');
    }
    state.auth.authenticated = false;
    showLoginOverlay();
    if (onLoggedOut) onLoggedOut();
}
