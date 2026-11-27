// Tokens: table, create modal, one-time reveal, rotate and delete.

import { api } from '../api.js';
import { formatDateTime, shortId } from '../format.js';
import {
    badge,
    button,
    card,
    confirmAction,
    copyText,
    dataTable,
    el,
    emptyState,
    errorState,
    modal,
    pageHeader,
    textField,
    toast,
} from '../ui.js';

/** One-time reveal dialog. Shared with the Users screen. */
export function revealToken(value, label) {
    const dialog = modal({
        title: 'Token created',
        body: [
            el('div', { className: 'token-reveal' }, [
                el('div', { className: 'token-reveal-label', text: label }),
                el('div', { className: 'token-reveal-value', text: value }),
                el('div', { className: 'chip-row' }, [
                    button({
                        label: 'Copy to clipboard',
                        icon: 'copy',
                        onClick: async () => {
                            try {
                                await copyText(value);
                                toast('Token copied to clipboard');
                            } catch {
                                toast('Could not copy — select and copy manually', 'error');
                            }
                        },
                    }),
                ]),
            ]),
            el('p', { className: 'muted', text: 'Copy it now; the value is not shown again.' }),
        ],
        actions: [{ label: 'Dismiss', variant: 'primary', onClick: () => dialog.close() }],
    });
    dialog.open();
}

function createDialog(onCreated) {
    const userId = textField({ label: 'User ID', placeholder: 'agent-alice' });
    const label = textField({ label: 'Label', placeholder: 'Description' });
    const supertoken = el('input', { type: 'checkbox' });
    const userIdInput = userId.querySelector('input');
    supertoken.addEventListener('change', () => {
        userIdInput.disabled = supertoken.checked;
        if (supertoken.checked) userIdInput.value = '';
    });
    let dialog;
    dialog = modal({
        title: 'Create token',
        body: [
            el('label', { className: 'checkbox-field' }, supertoken, 'Supertoken (not bound to an agent)'),
            userId,
            label,
        ],
        actions: [
            { label: 'Cancel', onClick: () => dialog.close() },
            {
                label: 'Create',
                variant: 'primary',
                onClick: async () => {
                    const id = userIdInput.value.trim();
                    const isSuper = supertoken.checked;
                    if (!isSuper && !id) {
                        toast('User ID is required unless creating a supertoken', 'error');
                        return;
                    }
                    try {
                        const result = await api.createToken(id, label.querySelector('input').value.trim(), isSuper);
                        dialog.close();
                        revealToken(result.token.token, isSuper ? 'Supertoken created' : `Token for ${id}`);
                        onCreated();
                    } catch (error) {
                        toast(error.message || 'Failed to create token', 'error');
                    }
                },
            },
        ],
    });
    dialog.open();
}

export async function render(route, ctx) {
    let data;
    try {
        data = await api.listTokens();
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    async function remove(token) {
        const confirmed = await confirmAction(
            `Delete token ${shortId(token.id)}? Agents using it are locked out immediately.`,
            { title: 'Delete token', confirmLabel: 'Delete' },
        );
        if (!confirmed) return;
        try {
            await api.deleteToken(token.id);
            toast('Token deleted');
            ctx.refresh();
        } catch (error) {
            toast(error.message || 'Failed to delete token', 'error');
        }
    }

    async function rotate(token) {
        const confirmed = await confirmAction(
            `Rotate token ${shortId(token.id)}? The old value stops working immediately.`,
            { title: 'Rotate token', confirmLabel: 'Rotate' },
        );
        if (!confirmed) return;
        try {
            const result = await api.rotateToken(token.id);
            revealToken(result.token.token, 'Rotated token');
            ctx.refresh();
        } catch (error) {
            toast(error.message || 'Failed to rotate token', 'error');
        }
    }

    const main = el(
        'div',
        {},
        pageHeader('Tokens', {
            subtitle: data.enabled ? `${data.tokens.length} tokens configured` : 'Token auth not configured',
            actions: data.enabled
                ? [{ label: 'Create token', icon: 'plus', variant: 'primary', onClick: () => createDialog(() => ctx.refresh()) }]
                : [],
        }),
        card(
            'All tokens',
            data.enabled
                ? dataTable({
                      columns: [
                          { label: 'ID', render: (token) => el('span', { className: 'mono', text: shortId(token.id) }) },
                          {
                              label: 'User',
                              render: (token) =>
                                  token.user_id ? el('span', { text: token.user_id }) : badge('supertoken', 'warning'),
                          },
                          { label: 'Label', render: (token) => token.label || el('span', { className: 'muted', text: '—' }) },
                          {
                              label: 'Prefix',
                              render: (token) => el('span', { className: 'mono muted', text: `${token.token_prefix}…` }),
                          },
                          { label: 'Created', render: (token) => formatDateTime(token.created_at) },
                          { label: 'Last used', render: (token) => formatDateTime(token.last_used_at) },
                          {
                              label: 'Actions',
                              render: (token) =>
                                  el(
                                      'div',
                                      { className: 'chip-row' },
                                      button({ label: 'Rotate', icon: 'rotate', onClick: () => rotate(token) }),
                                      button({ label: 'Delete', variant: 'danger', icon: 'trash', onClick: () => remove(token) }),
                                  ),
                          },
                      ],
                      rows: data.tokens,
                      empty: emptyState(
                          'No tokens yet',
                          'The API rejects requests until a token exists.',
                          data.enabled
                              ? button({ label: 'Create token', variant: 'primary', onClick: () => createDialog(() => ctx.refresh()) })
                              : null,
                      ),
                      rowKey: (token) => token.id,
                  })
                : emptyState('Token auth is disabled', 'Start the server with a token store to manage tokens.'),
        ),
    );
    return { main, detail: null };
}
