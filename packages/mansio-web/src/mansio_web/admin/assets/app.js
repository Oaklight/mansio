// Application shell: sidebar, topbar, router wiring.

import { api } from './api.js';
import { hideLoginOverlay, initAuthForm, logout, showLoginOverlay } from './auth.js';
import { formatCount } from './format.js';
import { navigate, parseHash, startRouter } from './router.js';
import { setConnection, state } from './state.js';
import {
    button,
    el,
    errorState,
    icon,
    loadingState,
    searchField,
    setActiveNav,
    toast,
} from './ui.js';
import * as channels from './views/channels.js';
import * as messages from './views/messages.js';
import * as overview from './views/overview.js';
import * as prefixes from './views/prefixes.js';
import * as subscriptions from './views/subscriptions.js';
import * as system from './views/system.js';
import * as tokens from './views/tokens.js';
import * as users from './views/users.js';

const VIEWS = { overview, channels, messages, subscriptions, users, tokens, prefixes, system };

const NAV = [
    {
        title: 'Workspace',
        items: [{ name: 'overview', label: 'Overview', icon: 'dashboard' }],
    },
    {
        title: 'Traffic',
        items: [
            { name: 'channels', label: 'Channels', icon: 'channel', badge: 'channels' },
            { name: 'messages', label: 'Messages', icon: 'message' },
            { name: 'subscriptions', label: 'Subscriptions', icon: 'bell' },
        ],
    },
    {
        title: 'Identity',
        items: [
            { name: 'users', label: 'Users', icon: 'users' },
            { name: 'tokens', label: 'Tokens', icon: 'key' },
            { name: 'prefixes', label: 'Prefixes', icon: 'tag' },
        ],
    },
    {
        title: 'System',
        items: [{ name: 'system', label: 'System', icon: 'system' }],
    },
];

const TITLES = {
    overview: 'Overview',
    channels: 'Channels',
    messages: 'Messages',
    subscriptions: 'Subscriptions',
    users: 'Users',
    tokens: 'Tokens',
    prefixes: 'Prefixes',
    system: 'System',
};

const THEME_BUTTONS = ['light', 'auto', 'dark'];
let currentCleanup = null;
let routerStarted = false;
let connectionPill = null;

function connectionLabel() {
    if (!connectionPill) return;
    connectionPill.className = `status-pill status-${state.connection === 'online' ? 'online' : state.connection === 'offline' ? 'offline' : 'muted'}`;
    connectionPill.replaceChildren(
        el('span', { className: 'status-dot' }),
        el('span', { className: 'connection-label', text: state.connection }),
    );
}

function updateConnection(value) {
    setConnection(value);
    connectionLabel();
}

function renderSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    sidebar.replaceChildren(
        ...NAV.map((section) =>
            el(
                'div',
                { className: 'nav-section' },
                el('div', { className: 'nav-section-title', text: section.title }),
                el(
                    'ul',
                    { className: 'nav-list' },
                    section.items.map((item) =>
                        el(
                            'li',
                            {},
                            el(
                                'a',
                                {
                                    className: 'nav-link',
                                    href: `#/${item.name}`,
                                    dataset: { nav: item.name },
                                },
                                icon(item.icon, 15),
                                el('span', { className: 'nav-label', text: item.label }),
                                item.badge === 'channels' && state.channels.length
                                    ? el('span', { className: 'nav-badge', text: formatCount(state.channels.length) })
                                    : null,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        el('div', { className: 'sidebar-footer', text: 'Mansio admin console' }),
    );
    setActiveNav(state.route && state.route.name);
}

function wireTheme() {
    const switcher = document.getElementById('themeSwitcher');
    if (!switcher) return;
    switcher.addEventListener('click', (event) => {
        const target = event.target.closest('button[data-theme]');
        if (!target) return;
        setTheme(target.dataset.theme);
    });
}

function updateThemeSwitcher() {
    const current = localStorage.getItem('mansio-theme') || 'auto';
    document.querySelectorAll('#themeSwitcher button').forEach((buttonNode) => {
        buttonNode.classList.toggle('active', buttonNode.dataset.theme === current);
    });
}

function setTheme(theme) {
    if (THEME_BUTTONS.includes(theme)) localStorage.setItem('mansio-theme', theme);
    else localStorage.removeItem('mansio-theme');
    if (theme === 'dark' || theme === 'light') {
        document.documentElement.dataset.theme = theme;
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
    updateThemeSwitcher();
    if (state.route && state.route.name === 'overview') refresh();
}

function renderTopbar() {
    const actions = document.getElementById('topbarActions');
    if (!actions) return;
    connectionPill = el('span', { className: 'status-pill status-muted' });
    connectionLabel();
    const logoutButton = button({
        label: 'Log out',
        ariaLabel: 'Log out',
        onClick: () => logout(() => showLoginOverlay()),
    });
    logoutButton.id = 'logoutButton';
    logoutButton.classList.toggle('hidden', !state.auth.required);
    actions.replaceChildren(
        connectionPill,
        button({ icon: 'refresh', ariaLabel: 'Refresh', onClick: () => refresh() }),
        button({
            icon: 'settings',
            ariaLabel: 'Settings',
            onClick: () => {
                const popup = document.getElementById('settingsPopup');
                if (popup) popup.classList.toggle('hidden');
                updateThemeSwitcher();
            },
        }),
        logoutButton,
    );
}

function wireGlobalSearch() {
    const host = document.getElementById('globalSearch');
    if (!host) return;
    const field = searchField({
        placeholder: 'Jump to channel…',
        onInput: () => {},
    });
    const input = field.querySelector('input');
    input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        const query = input.value.trim().toLowerCase();
        if (!query) return;
        const match = state.channels.find((channel) => channel.name.toLowerCase().includes(query));
        if (match) {
            input.value = '';
            navigate('channels', { id: match.name });
        } else {
            toast('No channel matches that search', 'error');
        }
    });
    host.replaceChildren(field);
}

function refresh() {
    renderRoute(state.route || parseHash());
}

function updateChrome(route) {
    state.route = route;
    setActiveNav(route.name);
    const title = TITLES[route.name] || TITLES.overview;
    const breadcrumb = document.getElementById('breadcrumb');
    if (breadcrumb) breadcrumb.textContent = route.id ? `${title} / ${route.id}` : title;
    document.title = `${route.id || title} · Mansio Admin`;
}

async function renderRoute(route) {
    updateChrome(route);
    if (state.auth.required && !state.auth.authenticated) {
        showLoginOverlay();
        return;
    }
    const main = document.getElementById('mainPane');
    const detailSlot = document.getElementById('detailPane');
    const shell = document.getElementById('appShell');
    if (!main || !detailSlot || !shell) return;

    if (currentCleanup) {
        currentCleanup();
        currentCleanup = null;
    }
    main.replaceChildren(loadingState('Loading…'));
    detailSlot.replaceChildren();
    shell.classList.remove('has-detail');

    const view = VIEWS[route.name] || VIEWS.overview;
    const ctx = {
        navigate: (name, options) => navigate(name, options),
        refresh: () => refresh(),
    };
    try {
        const result = await view.render({ ...route }, ctx);
        if (state.route !== route) return;
        main.replaceChildren(result.main || errorState('Empty view'));
        if (result.detail) {
            detailSlot.replaceChildren(result.detail);
            shell.classList.add('has-detail');
        }
        currentCleanup = result.cleanup || null;
        updateConnection('online');
    } catch (error) {
        main.replaceChildren(errorState(error, () => refresh()));
        updateConnection('offline');
    }
    renderSidebar();
}

function start() {
    if (!routerStarted) {
        routerStarted = true;
        startRouter(renderRoute);
    } else {
        refresh();
    }
}

async function init() {
    renderSidebar();
    renderTopbar();
    wireTheme();
    wireGlobalSearch();
    updateThemeSwitcher();
    api.onUnauthorized = () => {
        state.auth.authenticated = false;
        showLoginOverlay();
    };
    api.onConnection = (online) => updateConnection(online ? 'online' : 'offline');
    initAuthForm(() => {
        document.body.classList.remove('auth-pending');
        hideLoginOverlay();
        start();
    });

    try {
        const auth = await api.checkAuth();
        state.auth = {
            required: Boolean(auth.required),
            authenticated: auth.authenticated !== false,
        };
    } catch {
        state.auth = { required: false, authenticated: true };
    }
    renderTopbar();
    if (state.auth.required && !state.auth.authenticated) {
        showLoginOverlay();
        return;
    }
    document.body.classList.remove('auth-pending');
    start();
}

init();
