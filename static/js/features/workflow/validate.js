const {ref, computed} = window.Vue;
const {ElMessage} = window.ElementPlus;

export function createController(options) {
    const validationVisible = ref(false);
    const validationRulesVisible = ref(false);
    const validationLoading = ref(false);
    const validationData = ref(null);
    const validationError = ref('');
    const validationSortMode = ref('severity');
    const validationSearchQuery = ref('');

    const validationStatusRank = {issue: 3, warning: 2, info: 1, ok: 0};

    const validationStatusLabel = (status) => ({
        issue: 'Issue',
        warning: 'Warning',
        info: 'Normal',
        ok: 'OK',
    }[status] || String(status || '—'));

    const validationStatusType = (status) => ({
        issue: 'danger',
        warning: 'warning',
        info: 'info',
        ok: 'success',
    }[status] || 'info');

    const validationCountClass = (status) => ({
        issue: 'validation-count-issue',
        warning: 'validation-count-warning',
    }[status] || '');

    const sortedValidationSets = computed(() => {
        let items = Array.isArray(validationData.value?.sets)
            ? [...validationData.value.sets]
            : [];
        const query = String(validationSearchQuery.value || '').trim().toLocaleLowerCase();
        if (query) {
            items = items.filter((item) => String(item?.name || '').toLocaleLowerCase().includes(query));
        }
        if (validationSortMode.value === 'date') {
            return items.sort((a, b) => {
                const dateCompare = String(b.date_key || b.name || '').localeCompare(String(a.date_key || a.name || ''));
                if (dateCompare) return dateCompare;
                return String(b.name || '').localeCompare(String(a.name || ''));
            });
        }
        return items.sort((a, b) => {
            const rankCompare = (validationStatusRank[b.status] ?? -1) - (validationStatusRank[a.status] ?? -1);
            if (rankCompare) return rankCompare;
            const dateCompare = String(b.date_key || b.name || '').localeCompare(String(a.date_key || a.name || ''));
            if (dateCompare) return dateCompare;
            return String(a.name || '').localeCompare(String(b.name || ''));
        });
    });

    const fetchJson = async (url, init = undefined) => {
        const response = await fetch(url, init);
        let data;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error(`Server returned a non-JSON response (${response.status})`);
        }
        if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
        return data;
    };

    const openValidation = async () => {
        const source = options.getSource && options.getSource();
        if (!source || !source.id) {
            ElMessage.error('Library Source unavailable');
            return;
        }
        validationVisible.value = true;
        validationLoading.value = true;
        validationData.value = null;
        validationError.value = '';
        validationSearchQuery.value = '';
        try {
            validationData.value = await fetchJson(
                `/api/library/insights/sources/${source.id}/validate-root`,
                {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}
            );
        } catch (error) {
            validationError.value = error.message || 'Validation failed';
            ElMessage.error(validationError.value);
        } finally {
            validationLoading.value = false;
        }
    };

    return {
        validationVisible,
        validationRulesVisible,
        validationLoading,
        validationData,
        validationError,
        validationSortMode,
        validationSearchQuery,
        sortedValidationSets,
        validationStatusLabel,
        validationStatusType,
        validationCountClass,
        openValidation,
    };
}
