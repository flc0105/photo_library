(function (global) {
    const clean = (value) => String(value ?? '').trim();
    const keyOf = (value) => clean(value).toLocaleLowerCase();
    const empty = (value) => value === null || value === undefined || value === '';

    function createController({ getSourceId, getManifestForm, message }) {
        let sourceId = null;
        let loaded = false;
        let loadingPromise = null;
        let locations = [];
        let sources = [];
        let values = {};

        const reset = () => {
            sourceId = null;
            loaded = false;
            loadingPromise = null;
            locations = [];
            sources = [];
            values = {};
        };

        const load = async () => {
            const nextSourceId = getSourceId && getSourceId();
            if (!nextSourceId) return;
            if (nextSourceId !== sourceId) reset();
            sourceId = nextSourceId;
            if (loaded) return;
            if (loadingPromise) return loadingPromise;

            loadingPromise = (async () => {
                try {
                    const response = await fetch(`/api/library/sources/${nextSourceId}/manifest-reference`);
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to load manifest references');
                    locations = Array.isArray(data.locations) ? data.locations : [];
                    sources = Array.isArray(data.sources) ? data.sources : [];
                    values = data.values && typeof data.values === 'object' ? data.values : {};
                    loaded = true;
                } catch (error) {
                    // Autofill is convenience only. Do not block manifest editing.
                    if (message && message.warning) message.warning(error.message || 'Manifest autofill data unavailable');
                } finally {
                    loadingPromise = null;
                }
            })();
            return loadingPromise;
        };

        const locationMatch = (name) => {
            const key = keyOf(name);
            return key ? locations.find(item => keyOf(item.name) === key) : null;
        };

        const sourceMatch = (title) => {
            const key = keyOf(title);
            return key ? sources.find(item => keyOf(item.title) === key) : null;
        };

        const applyLocation = (item, overwrite = true) => {
            const form = getManifestForm && getManifestForm();
            if (!item || !form || !form.location) return false;
            form.location.name = item.name || form.location.name || '';
            for (const field of ['address', 'lat', 'lng']) {
                if ((overwrite || empty(form.location[field])) && !empty(item[field])) {
                    form.location[field] = item[field];
                }
            }
            return true;
        };

        const applySource = (item, overwrite = true) => {
            const form = getManifestForm && getManifestForm();
            if (!item || !form || !form.theme) return false;
            form.theme.source_title = item.title || form.theme.source_title || '';
            if ((overwrite || empty(form.theme.source_type)) && !empty(item.source_type)) {
                form.theme.source_type = item.source_type;
            }
            return true;
        };

        const syncLocation = async (name, overwrite = true) => {
            await load();
            return applyLocation(locationMatch(name), overwrite);
        };

        const syncSource = async (title, overwrite = true) => {
            await load();
            return applySource(sourceMatch(title), overwrite);
        };

        const queryLocations = async (query, callback) => {
            await load();
            const needle = keyOf(query);
            const result = locations
                .filter(item => !needle || keyOf(item.name).includes(needle))
                .slice(0, 30)
                .map(item => ({ ...item, value: item.name }));
            callback(result);
        };

        const querySources = async (query, callback) => {
            await load();
            const needle = keyOf(query);
            const result = sources
                .filter(item => !needle || keyOf(item.title).includes(needle))
                .slice(0, 30)
                .map(item => ({ ...item, value: item.title }));
            callback(result);
        };

        const queryValues = async (kind, query, callback) => {
            await load();
            const needle = keyOf(query);
            const options = Array.isArray(values[kind]) ? values[kind] : [];
            const result = options
                .filter(value => !needle || keyOf(value).includes(needle))
                .slice(0, 50)
                .map(value => ({ value }));
            callback(result);
        };

        const getValues = () => {
            const result = {};
            Object.entries(values || {}).forEach(([key, items]) => {
                result[key] = Array.isArray(items) ? [...items] : [];
            });
            return result;
        };

        const selectLocation = (item) => applyLocation(item, true);
        const selectSource = (item) => applySource(item, true);

        const syncCurrent = async () => {
            await load();
            const form = getManifestForm && getManifestForm();
            if (!form) return;
            if (form.location && form.location.name) applyLocation(locationMatch(form.location.name), false);
            if (form.theme && form.theme.source_title) applySource(sourceMatch(form.theme.source_title), false);
        };

        return {
            load,
            reset,
            invalidate: reset,
            queryLocations,
            querySources,
            queryValues,
            getValues,
            selectLocation,
            selectSource,
            syncLocation,
            syncSource,
            syncCurrent
        };
    }

    global.ManifestAutofill = { createController };
})(window);
