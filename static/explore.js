(() => {
    const requestJson = async (url, options = {}) => {
        const response = await fetch(url, options);
        let data = {};
        try {
            data = await response.json();
        } catch (_) {
            data = {};
        }
        if (!response.ok) {
            const error = new Error(data.error || `Request failed (${response.status})`);
            error.payload = data;
            error.status = response.status;
            throw error;
        }
        return data;
    };

    window.ExploreApi = {
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
        async queryBlock(blockId, bucketId, target = 'sets') {
            return requestJson(`/api/explore/blocks/${blockId}/query`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({bucket_id: bucketId, target}),
            });
        },
    };
})();
