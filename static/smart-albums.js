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

    const jsonOptions = (method, body) => ({
        method,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {})
    });

    window.SmartAlbumApi = {
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
        async refreshIndex() {
            return requestJson('/api/smart-albums/index/refresh', {method: 'POST'});
        },
        async indexProgress() {
            return requestJson('/api/smart-albums/index/progress');
        },
        async indexStatus() {
            return requestJson('/api/smart-albums/index/status');
        }
    };
})();
