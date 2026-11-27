// Users: list, registration, per-user tokens and deletion.

import { api } from '../api.js';
import { formatDateTime, shortId } from '../format.js';
import { navigate } from '../router.js';
import {
    button,
    card,
    confirmAction,
    dataTable,
    detailPane,
    el,
    emptyState,
    errorState,
    modal,
    pageHeader,
    statusPill,
    textField,
    toast,
} from '../ui.js';
import { revealToken } from './tokens.js';

function registerDialog(onCreated) {
    const userId = textField({ label: 'User ID', placeholder: 'agent-alice' });
    const label = textField({ label: 'Label', placeholder: 'initial token' });
    let dialog;
    dialog = modal({
        title: 'Register user',
        body: [userId, label],
        actions: [
            { label: 'Cancel', onClick: () => dialog.close() },
            {
                label: 'Register',
                variant: 'primary',
                onClick: async () => {
                    const id = userId.querySelector('input').value.trim();
                    if (!id) {
                        toast('User ID is required', 'error');
                        return;
                    }
                    try {
                        const result = await api.registerUser(id, label.querySelector('input').value.trim());
                        dialog.close();
                        revealToken(result.token.token, `Initial token for ${result.user_id}`);
                        onCreated();
                    } catch (error) {
                        toast(error.message || 'Failed to register user', 'error');
                    }
                },
            },
        ],
    });
    dialog.open();
}

async function userDetail(userId, ctx) {
    try {
        const detail = await api.getUser(userId);
        const addToken = async () => {
            let dialog;
            const label = textField({ label: 'Label', placeholder: 'additional token' });
            dialog = modal({
                title: `Add token for ${userId}`,
                body: [label],
                actions: [
                    { label: 'Cancel', onClick: () => dialog.close() },
                    {
                        label: 'Create',
                        variant: 'primary',
                        onClick: async () => {
                            try {
                                const result = await api.createUserToken(
                                    userId,
                                    label.querySelector('input').value.trim(),
                                );
                                dialog.close();
                                revealToken(result.token.token, `Token for ${userId}`);
                                ctx.refresh();
                            } catch (error) {
                                toast(error.message || 'Failed to create token', 'error');
                            }
                        },
                    },
                ],
            });
            dialog.open();
        };
        const removeUser = async () => {
            const confirmed = await confirmAction(
                `Delete ${userId} and all ${detail.tokens.length} token(s)? Agents using them are locked out.`,
                { title: 'Delete user', confirmLabel: 'Delete' },
            );
            if (!confirmed) return;
            try {
                const result = await api.deleteUser(userId);
                toast(`User deleted (${result.deleted_tokens} tokens)`);
                navigate('users');
            } catch (error) {
                toast(error.message || 'Failed to delete user', 'error');
            }
        };
        return detailPane({
            title: userId,
            subtitle: `Last seen ${formatDateTime(detail.last_seen)}`,
            actions: [
                { label: 'Add token', icon: 'plus', onClick: addToken },
                { label: 'Delete user', variant: 'danger', icon: 'trash', onClick: removeUser },
            ],
            onClose: () => navigate('users'),
            children: [
                el(
                    'div',
                    { className: 'detail-section' },
                    el('div', { className: 'detail-section-title', text: 'Status' }),
                    el(
                        'div',
                        { className: 'chip-row' },
                        statusPill(detail.status),
                    ),
                ),
                el(
                    'div',
                    { className: 'detail-section' },
                    el('div', { className: 'detail-section-title', text: `Tokens (${detail.tokens.length})` }),
                    dataTable({
                        columns: [
                            { label: 'Prefix', render: (token) => el('span', { className: 'mono', text: `${token.token_prefix}…` }) },
                            { label: 'Label', render: (token) => token.label || '—' },
                            { label: 'Created', render: (token) => formatDateTime(token.created_at) },
                            { label: 'Last used', render: (token) => formatDateTime(token.last_used_at) },
                        ],
                        rows: detail.tokens,
                        empty: emptyState('No tokens'),
                        rowKey: (token) => token.id,
                    }),
                ),
                el('p', { className: 'muted', text: `User ID: ${shortId(userId)}` }),
            ],
        });
    } catch (error) {
        return detailPane({
            title: userId,
            onClose: () => navigate('users'),
            children: errorState(error, () => ctx.refresh()),
        });
    }
}

export async function render(route, ctx) {
    let data;
    try {
        data = await api.listUsers();
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    const main = el(
        'div',
        {},
        pageHeader('Users', {
            subtitle: data.enabled ? `${data.users.length} users registered` : 'Token store not configured',
            actions: data.enabled
                ? [{ label: 'Register user', icon: 'plus', variant: 'primary', onClick: () => registerDialog(() => ctx.refresh()) }]
                : [],
        }),
        card(
            'Registered users',
            data.enabled
                ? dataTable({
                      columns: [
                          {
                              label: 'User',
                              render: (user) => el('span', { className: 'mono', text: user.user_id }),
                          },
                          { label: 'Status', render: (user) => statusPill(user.status) },
                          { label: 'Tokens', className: 'num', render: (user) => String(user.token_count) },
                          { label: 'Last active', render: (user) => formatDateTime(user.last_active) },
                          { label: 'Last seen', render: (user) => formatDateTime(user.last_seen) },
                          {
                              label: '',
                              render: (user) =>
                                  button({
                                      label: 'Open',
                                      onClick: () => ctx.navigate('users', { id: user.user_id }),
                                  }),
                          },
                      ],
                      rows: data.users,
                      empty: emptyState(
                          'No users yet',
                          'Register a user to issue its first token.',
                          data.enabled
                              ? button({ label: 'Register user', variant: 'primary', onClick: () => registerDialog(() => ctx.refresh()) })
                              : null,
                      ),
                      rowKey: (user) => user.user_id,
                  })
                : emptyState('User management is disabled', 'Start the server with a token store to manage users.'),
        ),
    );

    let detail = null;
    if (route.id) detail = await userDetail(route.id, ctx);
    return { main, detail };
}
