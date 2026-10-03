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

    window.SmartSetApi = {
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
})();
