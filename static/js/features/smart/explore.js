import {requestJson} from '../../core/http.js';

export const ExploreApi = {
        async stats() {
            return requestJson('/api/explore/stats');
        },
        async query(dimension, value, label, target = 'photos') {
            return requestJson('/api/explore/query', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({dimension, value, label, target}),
            });
        },
        async blocks() {
            return requestJson('/api/explore/blocks');
        },
        async reorderBlocks(blockIds) {
            return requestJson('/api/explore/blocks/reorder', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({block_ids: blockIds}),
            });
        },
        async createBlock(payload) {
            return requestJson('/api/explore/blocks', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
        },
        async updateBlock(blockId, payload) {
            return requestJson(`/api/explore/blocks/${blockId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
        },
        async deleteBlock(blockId) {
            return requestJson(`/api/explore/blocks/${blockId}`, {method: 'DELETE'});
        },
        async previewBlock(payload) {
            return requestJson('/api/explore/blocks/preview', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
        },
        async runtime() {
            return requestJson('/api/explore/runtime');
        },
        async blockStats(blockId) {
            return requestJson(`/api/explore/blocks/${blockId}/stats`);
        },
        async queryBlock(blockId, bucketId, target = 'sets') {
            return requestJson(`/api/explore/blocks/${blockId}/query`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({bucket_id: bucketId, target}),
            });
        },
    };
