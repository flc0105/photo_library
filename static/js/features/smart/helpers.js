import {jsonOptions, requestJson} from '../../core/http.js';

export const SmartHelpersApi = {
    async get() {
        return requestJson('/api/smart-helpers');
    },
    async validate(source) {
        return requestJson('/api/smart-helpers/validate', jsonOptions('POST', {source}));
    },
    async save(source) {
        return requestJson('/api/smart-helpers', jsonOptions('PUT', {source}));
    },
};
