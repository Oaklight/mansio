// Shared, read-mostly UI state.

export const state = {
    /** @type {'connecting'|'online'|'offline'} */
    connection: 'connecting',
    /** @type {{required: boolean, authenticated: boolean}} */
    auth: { required: false, authenticated: true },
    /** @type {Object|null} parsed hash route */
    route: null,
    /** @type {Array<Object>} channel summaries for pickers */
    channels: [],
};

/** @param {'connecting'|'online'|'offline'} value */
export function setConnection(value) {
    state.connection = value;
}
