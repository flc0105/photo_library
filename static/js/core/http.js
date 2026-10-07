export const requestJson = async (url, options = {}) => {
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

export const jsonOptions = (method, body) => ({
    method,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})
});
