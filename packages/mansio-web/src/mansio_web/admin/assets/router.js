// Hash router. Unknown routes fall back to Overview.

export const DEFAULT_ROUTE = 'overview';

const KNOWN = new Set([
    'overview',
    'channels',
    'messages',
    'subscriptions',
    'users',
    'tokens',
    'prefixes',
    'system',
]);

/**
 * @typedef {Object} Route
 * @property {string} name
 * @property {string|null} id
 * @property {URLSearchParams} query
 */

/** @returns {Route} */
export function parseHash(hash = window.location.hash) {
    const raw = hash.replace(/^#\/?/, '');
    const [pathPart, queryPart = ''] = raw.split('?');
    const segments = pathPart.split('/').filter(Boolean);
    const name = KNOWN.has(segments[0]) ? segments[0] : DEFAULT_ROUTE;
    let id = null;
    if (segments[1]) {
        try {
            id = decodeURIComponent(segments[1]);
        } catch {
            // A truncated percent sequence must not stop the app from booting.
            id = segments[1];
        }
    }
    return { name, id, query: new URLSearchParams(queryPart) };
}

export function href(name, { id = null, query = null } = {}) {
    const parts = [name];
    if (id) parts.push(encodeURIComponent(id));
    const search = query ? new URLSearchParams(query).toString() : '';
    return `#/${parts.join('/')}${search ? `?${search}` : ''}`;
}

export function navigate(name, options = {}, { replace = false } = {}) {
    const target = href(name, options);
    if (replace) {
        window.history.replaceState(null, '', target);
        window.dispatchEvent(new HashChangeEvent('hashchange'));
    } else if (window.location.hash !== target) {
        window.location.hash = target;
    }
}

/** Subscribe to route changes; renders the current route once. */
export function startRouter(onRoute) {
    const run = () => onRoute(parseHash());
    window.addEventListener('hashchange', run);
    run();
}
