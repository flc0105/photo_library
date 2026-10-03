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
    };
})();
