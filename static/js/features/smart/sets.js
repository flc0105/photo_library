import {jsonOptions, requestJson} from '../../core/http.js';

export const SmartSetApi = {
        async list() {
            return requestJson('/api/smart-sets');
        },
        async runtime() {
            return requestJson('/api/smart-sets/runtime');
        },
        async create(payload) {
            return requestJson('/api/smart-sets', jsonOptions('POST', payload));
        },
        async update(id, payload) {
            return requestJson(`/api/smart-sets/${id}`, jsonOptions('PUT', payload));
        },
        async remove(id) {
            return requestJson(`/api/smart-sets/${id}`, {method: 'DELETE'});
        },
        async run(id) {
            return requestJson(`/api/smart-sets/${id}/query`, {method: 'POST'});
        }
    };
