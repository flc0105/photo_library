'use strict';

const ACCEPT = '.jpg,.jpeg,.heic,.heif,.tif,.tiff,.png,image/jpeg,image/heic,image/heif,image/tiff,image/png';
const MAX_BYTES = 100 * 1024 * 1024;

const pickPhoto = () => new Promise((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = ACCEPT;
    // Keep the native file input in the document without display:none.
    // Some browsers are less reliable opening a picker for a display:none input.
    input.style.position = 'fixed';
    input.style.left = '-10000px';
    input.style.top = '0';
    input.style.width = '1px';
    input.style.height = '1px';
    input.style.opacity = '0';
    input.style.pointerEvents = 'none';
    document.body.appendChild(input);

    let settled = false;
    const cleanup = () => {
        input.remove();
    };
    const finish = (file) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(file || null);
    };
    const fail = (error) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
    };

    input.addEventListener('change', () => {
        finish(input.files && input.files.length ? input.files[0] : null);
    }, {once: true});

    // Modern Chromium/Safari expose a dedicated cancel event. Do not use the
    // old window-focus heuristic here: on macOS it can fire before the change
    // event and silently discard a file the user actually selected.
    input.addEventListener('cancel', () => finish(null), {once: true});

    try {
        if (typeof input.showPicker === 'function') {
            input.showPicker();
        } else {
            input.click();
        }
    } catch (error) {
        // showPicker can be unavailable/blocked even when input.click works.
        try {
            input.click();
        } catch (_) {
            fail(new Error('Photo picker unavailable.'));
        }
    }
});

const readResponse = async (response) => {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        try {
            return await response.json();
        } catch (_) {
            return {};
        }
    }

    let text = '';
    try {
        text = (await response.text()).trim();
    } catch (_) {
        // ignore
    }
    return {__nonJson: true, text};
};

const extractGps = async (file) => {
    if (!file) return null;
    if (file.size > MAX_BYTES) throw new Error('Photo exceeds 100 MB.');

    const body = new FormData();
    body.append('photo', file, file.name || 'iphone-photo.jpg');

    let response;
    try {
        response = await fetch('/api/tools/extract-gps', {
            method: 'POST',
            body
        });
    } catch (_) {
        throw new Error('GPS service unavailable.');
    }

    const data = await readResponse(response);

    if (!response.ok) {
        if (response.status === 404) {
            throw new Error('GPS endpoint unavailable.');
        }
        if (response.status === 401 || response.status === 403) {
            throw new Error('Admin access required.');
        }
        throw new Error(data.error || `GPS read failed (${response.status}).`);
    }

    if (data.__nonJson) {
        throw new Error('Invalid GPS response.');
    }

    const lat = Number(data.lat);
    const lng = Number(data.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
        throw new Error(data.error || 'Invalid photo GPS data.');
    }

    return {...data, lat, lng};
};

const createController = ({getLocation, message}) => {
    const loading = window.Vue.ref(false);

    const run = async () => {
        if (loading.value) return;

        let file;
        try {
            file = await pickPhoto();
        } catch (error) {
            if (message && message.error) {
                message.error(error?.message || 'Photo picker unavailable.');
            }
            return;
        }

        // Cancelling the native picker is not an error.
        if (!file) return;

        loading.value = true;
        if (message && message.info) {
            message.info(`Reading GPS: ${file.name}`);
        }

        try {
            const gps = await extractGps(file);
            const location = getLocation && getLocation();
            if (!location) throw new Error('Location form unavailable.');

            // Backend already rounds to 5 decimals; keep numeric JSON values.
            location.lat = Number(gps.lat.toFixed(5));
            location.lng = Number(gps.lng.toFixed(5));

            if (message && message.success) {
                message.success(`GPS set: ${gps.lat.toFixed(5)}, ${gps.lng.toFixed(5)}`);
            }
        } catch (error) {
            if (message && message.error) {
                message.error(error?.message || 'GPS read failed.');
            }
        } finally {
            loading.value = false;
        }
    };

    return {loading, run};
};

export {createController};
