import {jsonOptions, requestJson} from '../../core/http.js';

export const SmartAlbumApi = {
        async list() {
            return requestJson('/api/smart-albums');
        },
        async runtime() {
            return requestJson('/api/smart-albums/runtime');
        },
        async create(payload) {
            return requestJson('/api/smart-albums', jsonOptions('POST', payload));
        },
        async update(id, payload) {
            return requestJson(`/api/smart-albums/${id}`, jsonOptions('PUT', payload));
        },
        async remove(id) {
            return requestJson(`/api/smart-albums/${id}`, {method: 'DELETE'});
        },
        async run(id) {
            return requestJson(`/api/smart-albums/${id}/query`, {method: 'POST'});
        },
        async refreshIndex(sourceIds) {
            return requestJson('/api/smart-albums/index/refresh', jsonOptions('POST', {source_ids: sourceIds}));
        },
        async cancelIndex() {
            return requestJson('/api/smart-albums/index/cancel', {method: 'POST'});
        },
        async indexProgress() {
            return requestJson('/api/smart-albums/index/progress');
        },
        async indexStatus() {
            return requestJson('/api/smart-albums/index/status');
        },
        async previewIndexSync(sourceIds) {
            return requestJson('/api/smart-albums/index/sync/preview', jsonOptions('POST', {source_ids: sourceIds}));
        },
        async startIndexSync(planId) {
            return requestJson('/api/smart-albums/index/sync/start', jsonOptions('POST', {plan_id: planId}));
        },
        async cancelIndexSync() {
            return requestJson('/api/smart-albums/index/sync/cancel', {method: 'POST'});
        },
        async indexSyncProgress() {
            return requestJson('/api/smart-albums/index/sync/progress');
        }
    };
