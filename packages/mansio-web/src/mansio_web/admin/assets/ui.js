// Component vocabulary for the admin console.
//
// Everything is built with DOM APIs: data goes through textContent or
// setAttribute, never innerHTML.

const SVG_NS = 'http://www.w3.org/2000/svg';

const ICONS = {
    refresh: ['polyline 23 4 23 10 17 10', 'polyline 1 20 1 14 7 14', 'path M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15'],
    search: ['circle 11 11 8', 'line 21 21 16.65 16.65'],
    plus: ['line 12 5 12 19', 'line 5 12 19 12'],
    trash: ['polyline 3 6 5 6 21 6', 'path M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'],
    rotate: ['polyline 23 4 23 10 17 10', 'path M20.49 15a9 9 0 1 1-2.12-9.36L23 10'],
    copy: ['rect 9 9 13 13 2 2', 'path M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'],
    close: ['line 18 6 6 18', 'line 6 6 18 18'],
    chevronRight: ['polyline 9 18 15 12 9 6'],
    chevronDown: ['polyline 6 9 12 15 18 9'],
    back: ['polyline 15 18 9 12 15 6'],
    dashboard: ['rect 3 3 7 7', 'rect 14 3 7 7', 'rect 14 14 7 7', 'rect 3 14 7 7'],
    channel: ['line 4 9 20 9', 'line 4 15 20 15', 'line 10 3 8 21', 'line 16 3 14 21'],
    message: ['path M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
    bell: ['path M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9', 'path M13.73 21a2 2 0 0 1-3.46 0'],
    users: ['path M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2', 'circle 9 7 4', 'path M23 21v-2a4 4 0 0 0-3-3.87', 'path M16 3.13a4 4 0 0 1 0 7.75'],
    key: ['path M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3'],
    tag: ['path M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z', 'line 7 7 7.01 7'],
    system: ['rect 2 2 20 8 2 2', 'rect 2 14 20 8 2 2', 'line 6 6 6.01 6', 'line 6 18 6.01 18'],
    settings: ['circle 12 12 3', 'path M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'],
    alert: ['path M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z', 'line 12 9 12 13', 'line 12 17 12.01 17'],
    check: ['polyline 20 6 9 17 4 12'],
};

function svgNode(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.classList.add('icon');
    for (const spec of ICONS[name] || []) {
        const [tag, ...coords] = spec.split(' ');
        const node = document.createElementNS(SVG_NS, tag);
        const numeric = coords.map((value) => (value === '' ? value : Number(value)));
        if (tag === 'circle') {
            node.setAttribute('cx', String(numeric[0]));
            node.setAttribute('cy', String(numeric[1]));
            node.setAttribute('r', String(numeric[2]));
        } else if (tag === 'rect') {
            node.setAttribute('x', String(numeric[0]));
            node.setAttribute('y', String(numeric[1]));
            node.setAttribute('width', String(numeric[2]));
            node.setAttribute('height', String(numeric[3]));
            if (numeric[4]) {
                node.setAttribute('rx', String(numeric[4]));
                node.setAttribute('ry', String(numeric[4]));
            }
        } else if (tag === 'line') {
            node.setAttribute('x1', String(numeric[0]));
            node.setAttribute('y1', String(numeric[1]));
            node.setAttribute('x2', String(numeric[2]));
            node.setAttribute('y2', String(numeric[3]));
        } else {
            node.setAttribute('d', coords.join(' '));
        }
        svg.appendChild(node);
    }
    return svg;
}

export function icon(name, size = 16) {
    const svg = svgNode(name);
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    return svg;
}

/**
 * Create an element.
 * @param {string} tag
 * @param {Object} [props]
 * @param {...(Node|string|Array|null|undefined)} children
 */
export function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
        if (value === undefined || value === null) continue;
        if (key === 'className') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key === 'on') {
            for (const [event, handler] of Object.entries(value)) {
                node.addEventListener(event, handler);
            }
        } else if (key === 'href') node.setAttribute('href', value);
        else if (key === 'value') node.value = value;
        else if (key === 'checked' || key === 'disabled' || key === 'selected') node[key] = Boolean(value);
        else if (key === 'type') node.setAttribute('type', value);
        else if (key === 'aria') {
            for (const [attr, attrValue] of Object.entries(value)) {
                node.setAttribute(`aria-${attr}`, attrValue);
            }
        } else node.setAttribute(key, value);
    }
    append(node, children);
    return node;
}

function append(node, children) {
    for (const child of children.flat(Infinity)) {
        if (child === null || child === undefined || child === false) continue;
        node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

export function replacement(node, ...children) {
    node.replaceChildren();
    append(node, children);
    return node;
}

// ── Layout ─────────────────────────────────────────────────────

export function pageHeader(title, { subtitle = '', actions = [] } = {}) {
    return el(
        'header',
        { className: 'page-header' },
        el(
            'div',
            { className: 'page-heading' },
            el('h1', { text: title }),
            subtitle ? el('p', { className: 'page-subtitle', text: subtitle }) : null,
        ),
        actions.length
            ? el('div', { className: 'page-actions' }, actions.map((action) => button(action)))
            : null,
    );
}

/**
 * @param {{label: string, onClick?: Function, variant?: string, icon?: string,
 *          ariaLabel?: string, disabled?: boolean}} spec
 */
export function button({ label, onClick, variant = 'secondary', icon: iconName, ariaLabel, disabled } = {}) {
    const node = el(
        'button',
        {
            className: `btn btn-${variant}`,
            type: 'button',
            disabled,
            'aria-label': ariaLabel || (iconName && !label ? label : undefined),
            on: onClick ? { click: onClick } : {},
        },
        iconName ? icon(iconName, 14) : null,
        label ? el('span', { text: label }) : null,
    );
    return node;
}

export function card(title, children, { actions = [], className = '' } = {}) {
    return el(
        'section',
        { className: `card ${className}`.trim() },
        title || actions.length
            ? el(
                  'div',
                  { className: 'card-header' },
                  el('h2', { className: 'card-title', text: title || '' }),
                  actions.length ? el('div', { className: 'card-actions' }, actions.map((a) => button(a))) : null,
              )
            : null,
        el('div', { className: 'card-body' }, children),
    );
}

export function statCard({ label, value, hint }) {
    return el(
        'div',
        { className: 'stat-card' },
        el('div', { className: 'stat-label', text: label }),
        el('div', { className: 'stat-value', text: String(value) }),
        hint ? el('div', { className: 'stat-hint', text: hint }) : null,
    );
}

export function statGrid(cards) {
    return el('div', { className: 'stat-grid' }, cards.map(statCard));
}

export function toolbar(children) {
    return el('div', { className: 'toolbar' }, children);
}

export function searchField({ placeholder = 'Search…', value = '', onInput }) {
    const input = el('input', {
        className: 'input search-input',
        type: 'search',
        placeholder,
        value,
        'aria-label': placeholder,
    });
    if (onInput) input.addEventListener('input', () => onInput(input.value));
    return el('label', { className: 'search-field' }, icon('search', 14), input);
}

export function selectField({ label, options, value = '', onChange, ariaLabel }) {
    const select = el(
        'select',
        { className: 'select', 'aria-label': ariaLabel || label || 'filter' },
        options.map((option) =>
            el(
                'option',
                { value: option.value, selected: option.value === value },
                option.label,
            ),
        ),
    );
    if (onChange) select.addEventListener('change', () => onChange(select.value));
    return label
        ? el('label', { className: 'select-field' }, el('span', { className: 'field-label', text: label }), select)
        : select;
}

export function textField({ label, value = '', placeholder = '', type = 'text', onInput, id }) {
    const input = el('input', { className: 'input', type, placeholder, value, id });
    if (onInput) input.addEventListener('input', () => onInput(input.value));
    return el('label', { className: 'field' }, label ? el('span', { className: 'field-label', text: label }) : null, input);
}

export function textArea({ label, value = '', placeholder = '', rows = 4, id }) {
    const area = el('textarea', { className: 'input textarea', placeholder, rows: String(rows), id });
    area.value = value;
    return el('label', { className: 'field' }, label ? el('span', { className: 'field-label', text: label }) : null, area);
}

// ── Data display ───────────────────────────────────────────────

export function badge(text, variant = '') {
    return el('span', { className: `badge ${variant ? `badge-${variant}` : ''}`.trim(), text });
}

export function statusPill(status) {
    const normalized = String(status || 'unknown').toLowerCase();
    const variant =
        normalized === 'online' || normalized === 'ok'
            ? 'online'
            : normalized === 'offline' || normalized === 'error'
              ? 'offline'
              : normalized === 'unknown'
                ? 'muted'
                : 'warn';
    return el(
        'span',
        { className: `status-pill status-${variant}` },
        el('span', { className: 'status-dot' }),
        el('span', { text: normalized }),
    );
}

/**
 * @param {{columns: Array<{label: string, render: Function, className?: string}>,
 *          rows: Array<Object>, empty?: Node, rowKey?: Function}} spec
 */
export function dataTable({ columns, rows, empty, rowKey }) {
    if (!rows.length && empty) return empty;
    const head = el(
        'tr',
        {},
        columns.map((column) => el('th', { className: column.className || '', text: column.label })),
    );
    const body = rows.map((row, index) =>
        el(
            'tr',
            { className: 'data-row', dataset: rowKey ? { key: rowKey(row) } : { index: String(index) } },
            columns.map((column) =>
                el('td', { className: column.className || '' }, column.render(row, index)),
            ),
        ),
    );
    return el(
        'div',
        { className: 'table-wrap' },
        el('table', { className: 'data-table' }, el('thead', {}, head), el('tbody', {}, body)),
    );
}

/** @param {{items: Array<Object>, render: Function, empty?: Node}} spec */
export function list({ items, render, empty }) {
    if (!items.length && empty) return empty;
    return el('div', { className: 'list', role: 'list' }, items.map((item) => render(item)));
}

export function listRow({ title, subtitle, meta, actions = [], href, onClick, active = false }) {
    const content = el(
        'div',
        { className: 'list-row-content' },
        el('div', { className: 'list-row-title', text: title }),
        subtitle ? el('div', { className: 'list-row-subtitle', text: subtitle }) : null,
    );
    const body = el(
        'div',
        { className: 'list-row-main' },
        content,
        meta ? el('div', { className: 'list-row-meta' }, meta) : null,
    );
    const tail = actions.length ? el('div', { className: 'list-row-actions' }, actions) : null;
    if (href) {
        return el(
            'a',
            { className: `list-row ${active ? 'active' : ''}`.trim(), href, role: 'listitem' },
            body,
            tail,
        );
    }
    return el(
        'button',
        {
            className: `list-row ${active ? 'active' : ''}`.trim(),
            type: 'button',
            role: 'listitem',
            on: onClick ? { click: onClick } : {},
        },
        body,
        tail,
    );
}

export function kvTable(rows) {
    return el(
        'table',
        { className: 'kv-table' },
        el(
            'tbody',
            {},
            rows.map(([key, value]) =>
                el(
                    'tr',
                    {},
                    el('th', { text: key }),
                    el('td', {}, typeof value === 'string' || typeof value === 'number' ? String(value) : value),
                ),
            ),
        ),
    );
}

export function detailPane({ title, subtitle, actions = [], onClose, children }) {
    return el(
        'aside',
        { className: 'detail-pane', 'aria-label': title },
        el(
            'div',
            { className: 'detail-header' },
            el(
                'div',
                {},
                el('h2', { className: 'detail-title', text: title }),
                subtitle ? el('div', { className: 'detail-subtitle', text: subtitle }) : null,
            ),
            el(
                'div',
                { className: 'detail-actions' },
                actions.map((a) => button(a)),
                onClose ? button({ icon: 'close', ariaLabel: 'Close detail', onClick: onClose }) : null,
            ),
        ),
        el('div', { className: 'detail-body' }, children),
    );
}

// ── States ─────────────────────────────────────────────────────

export function emptyState(title, hint = '', action = null) {
    return el(
        'div',
        { className: 'state-block empty-state' },
        el('div', { className: 'state-title', text: title }),
        hint ? el('div', { className: 'state-hint', text: hint }) : null,
        action ? el('div', { className: 'state-action' }, action) : null,
    );
}

export function errorState(error, retry) {
    return el(
        'div',
        { className: 'state-block error-state', role: 'alert' },
        icon('alert', 20),
        el('div', { className: 'state-title', text: 'Something went wrong' }),
        el('div', { className: 'state-hint', text: error && error.message ? error.message : String(error) }),
        retry ? el('div', { className: 'state-action' }, button({ label: 'Retry', icon: 'refresh', onClick: retry })) : null,
    );
}

export function loadingState(label = 'Loading…') {
    return el(
        'div',
        { className: 'state-block loading-state', role: 'status', 'aria-live': 'polite' },
        el('div', { className: 'state-title', text: label }),
    );
}

// ── Overlays ───────────────────────────────────────────────────

export function toast(message, type = 'success') {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const node = el('div', { className: `toast toast-${type}`, role: 'status', text: message });
    container.appendChild(node);
    window.setTimeout(() => {
        node.classList.add('leaving');
        window.setTimeout(() => node.remove(), 250);
    }, 3200);
}

const FOCUSABLE = [
    'a[href]', 'button:not([disabled])', 'input:not([disabled])', 'select:not([disabled])',
    'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * @param {{title: string, body: Node|string, actions?: Array<Object>,
 *          onClose?: Function, className?: string}} spec
 */
export function modal({ title, body, actions = [], onClose, className = '' }) {
    const previouslyFocused = document.activeElement;
    const dialog = el(
        'div',
        {
            className: `modal ${className}`.trim(),
            role: 'dialog',
            'aria-modal': 'true',
            'aria-label': title,
        },
        el(
            'div',
            { className: 'modal-header' },
            el('h2', { className: 'modal-title', text: title }),
            button({ icon: 'close', ariaLabel: 'Close dialog', onClick: () => close() }),
        ),
        el('div', { className: 'modal-body' }, typeof body === 'string' ? el('p', { text: body }) : body),
        actions.length ? el('div', { className: 'modal-actions' }, actions.map((a) => button(a))) : null,
    );
    const overlay = el('div', { className: 'modal-overlay' }, dialog);
    overlay.addEventListener('mousedown', (event) => {
        if (event.target === overlay) close();
    });

    function onKeydown(event) {
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
            return;
        }
        if (event.key !== 'Tab') return;
        const focusable = [...dialog.querySelectorAll(FOCUSABLE)].filter(
            (node) => node.offsetParent !== null,
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function close() {
        document.removeEventListener('keydown', onKeydown, true);
        overlay.remove();
        if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
            previouslyFocused.focus();
        }
        if (onClose) onClose();
    }

    function open() {
        document.body.appendChild(overlay);
        document.addEventListener('keydown', onKeydown, true);
        const firstField = dialog.querySelector('input, textarea, select');
        const first = firstField || dialog.querySelector(FOCUSABLE);
        if (first) first.focus();
    }

    return { open, close, element: overlay };
}

/** @returns {Promise<boolean>} */
export function confirmAction(message, { title = 'Are you sure?', confirmLabel = 'Confirm' } = {}) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            dialog.close();
            resolve(value);
        };
        const dialog = modal({
            title,
            body: el('p', { text: message }),
            actions: [
                { label: 'Cancel', variant: 'secondary', onClick: () => finish(false) },
                { label: confirmLabel, variant: 'danger', onClick: () => finish(true) },
            ],
            onClose: () => finish(false),
        });
        dialog.open();
    });
}

export function copyText(text) {
    return navigator.clipboard.writeText(text);
}

export function chartCanvas(className = 'throughput-chart') {
    return el('canvas', { className, 'aria-label': 'Throughput chart', role: 'img' });
}
