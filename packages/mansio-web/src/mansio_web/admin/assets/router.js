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

/**
 * Resolve a hash to the route to render.
 *
 * An unknown screen name or an id that is not a valid percent sequence
 * redirects to DEFAULT_ROUTE.  DEFAULT_ROUTE is in KNOWN, so the fallback
 * always resolves to itself and the redirect cannot loop.
 *
 * @param {string} [hash]
 * @returns {{route: Route, redirect: boolean}}
 */
export function resolveHash(hash = window.location.hash) {
    const raw = hash.replace(/^#\/?/, '');
    const [pathPart = ''] = raw.split('?');
    const segments = pathPart.split('/').filter(Boolean);
    const unknownScreen = segments.length > 0 && !KNOWN.has(segments[0]);
    let malformedId = false;
    if (segments[1]) {
        try {
            decodeURIComponent(segments[1]);
        } catch {
            malformedId = true;
        }
    }
    if (!unknownScreen && !malformedId) {
        return { route: parseHash(hash), redirect: false };
    }
    return { route: parseHash(href(DEFAULT_ROUTE)), redirect: true };
}

/** Subscribe to route changes; renders the current route once. */
export function startRouter(onRoute) {
    const run = () => {
        const { route, redirect } = resolveHash();
        if (redirect) window.history.replaceState(null, '', href(DEFAULT_ROUTE));
        onRoute(route);
    };
    window.addEventListener('hashchange', run);
    run();
}
