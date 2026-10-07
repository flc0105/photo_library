const {ref, computed} = window.Vue;
const {ElMessage, ElMessageBox} = window.ElementPlus;

function createController(options) {
    const previewVisible = ref(false);
    const previewLoading = ref(false);
    const previewKind = ref('');
    const previewTitle = ref('Advanced Feature');
    const previewData = ref(null);
    const threshold = ref(0.8);
    const protectOriginalsStatus = ref(null);
    let protectOriginalsStatusRequest = 0;

    const inspectionVisible = ref(false);
    const inspectionRulesVisible = ref(false);
    const inspectionLoading = ref(false);
    const inspectionData = ref(null);
    const inspectionError = ref('');
    const inspectionMetadataVisible = ref(false);
    const inspectionMetadataLoading = ref(false);
    const inspectionMetadataData = ref(null);
    const inspectionMetadataError = ref('');

    const photoImportVisible = ref(false);
    const photoImportLoading = ref(false);
    const photoImportExecuting = ref(false);
    const photoImportForm = ref({
        source_root: '',
        gap_minutes: 30
    });
    const photoImportPlan = ref(null);
    const photoImportError = ref('');
    const photoImportSelectedGroups = ref([]);
    const photoImportSourceRootConfigKey = 'photo_import_source_root';
    let photoImportSourceRootSaveTimer = null;

    const progressVisible = ref(false);
    const task = ref({
        id: '',
        kind: '',
        status: '',
        total: 0,
        completed: 0,
        current: 0,
        percent: 0,
        message: '',
        logs: [],
        result: null,
        error: null
    });
    let pollTimer = null;
    let refreshedTaskId = '';

    const context = () => {
        const source = options.getSource && options.getSource();
        const path = options.getSetPath && options.getSetPath();
        if (!source || !source.id || path === null || path === undefined) {
            throw new Error('No active Set');
        }
        return {sourceId: source.id, path};
    };

    const fetchJson = async (url, init) => {
        const response = await fetch(url, init);
        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error(`Server returned a non-JSON response (${response.status})`);
        }
        if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
        return data;
    };

    const postJson = (url, payload) => fetchJson(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload || {})
    });

    const refreshProtectOriginalsStatus = async () => {
        const requestId = ++protectOriginalsStatusRequest;
        protectOriginalsStatus.value = null;
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            protectOriginalsStatus.value = null;
            return;
        }
        try {
            const data = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/protect-originals/status`,
                {path: ctx.path}
            );
            if (requestId === protectOriginalsStatusRequest) {
                protectOriginalsStatus.value = data.summary || null;
            }
        } catch (error) {
            if (requestId === protectOriginalsStatusRequest) protectOriginalsStatus.value = null;
        }
    };

    const protectOriginalsLabel = computed(() => {
        if (protectOriginalsStatus.value?.all_protected) return 'Protect Originals ✓';
        if ((protectOriginalsStatus.value?.protected_original_count || 0) > 0) {
            return 'Protect Originals ◐';
        }
        return 'Protect Originals';
    });

    const handleWorkflowMenuVisible = (visible) => {
        if (visible) void refreshProtectOriginalsStatus();
    };

    const openImageInspection = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        inspectionData.value = null;
        inspectionError.value = '';
        inspectionVisible.value = true;
        inspectionLoading.value = true;
        try {
            inspectionData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/image-inspection`,
                {path: ctx.path}
            );
        } catch (error) {
            inspectionError.value = error.message || 'Image inspection failed';
            ElMessage.error(inspectionError.value);
        } finally {
            inspectionLoading.value = false;
        }
    };


    const openInspectionMetadata = async (item) => {
        if (!item || !item.relative_path) return;
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }

        inspectionMetadataData.value = null;
        inspectionMetadataError.value = '';
        inspectionMetadataVisible.value = true;
        inspectionMetadataLoading.value = true;
        try {
            inspectionMetadataData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/image-inspection/metadata`,
                {path: ctx.path, relative_path: item.relative_path}
            );
        } catch (error) {
            inspectionMetadataError.value = error.message || 'Metadata read failed';
            ElMessage.error(inspectionMetadataError.value);
        } finally {
            inspectionMetadataLoading.value = false;
        }
    };


    const analyzeVisualRename = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        const value = Number(threshold.value);
        if (!Number.isFinite(value) || value < 0 || value > 1) {
            ElMessage.error('Threshold must be between 0.0 and 1.0');
            return;
        }
        previewLoading.value = true;
        try {
            previewData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/visual-rename/preview`,
                {path: ctx.path, threshold: value}
            );
        } catch (error) {
            previewData.value = null;
            ElMessage.error(error.message || 'Match Rename preview failed');
        } finally {
            previewLoading.value = false;
        }
    };

    const openVisualRename = async () => {
        previewKind.value = 'visual_rename';
        previewTitle.value = 'Match Rename';
        threshold.value = 0.8;
        previewData.value = null;
        previewVisible.value = true;
        await analyzeVisualRename();
    };

    const openDiscardUnreturnedBase = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        previewKind.value = 'discard_unreturned_base';
        previewTitle.value = 'Discard Unreturned';
        previewData.value = null;
        previewVisible.value = true;
        previewLoading.value = true;
        try {
            previewData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/discard-unreturned/preview`,
                {path: ctx.path}
            );
        } catch (error) {
            previewData.value = null;
            ElMessage.error(error.message || 'Discard preview failed');
        } finally {
            previewLoading.value = false;
        }
    };

    const openSync = async (direction) => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        previewKind.value = 'sync_originals';
        previewTitle.value = direction === 'raw_by_jpg'
            ? 'Delete Extra RAW'
            : 'Delete Extra JPG';
        previewData.value = null;
        previewVisible.value = true;
        previewLoading.value = true;
        try {
            previewData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/sync/preview`,
                {path: ctx.path, direction}
            );
        } catch (error) {
            previewData.value = null;
            ElMessage.error(error.message || 'Sync preview failed');
        } finally {
            previewLoading.value = false;
        }
    };

    const openProtectOriginals = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        previewKind.value = 'protect_originals';
        previewTitle.value = 'Protect Originals';
        previewData.value = null;
        previewVisible.value = true;
        previewLoading.value = true;
        try {
            const data = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/protect-originals/preview`,
                {path: ctx.path}
            );
            previewData.value = data;
            protectOriginalsStatus.value = data.summary || null;
        } catch (error) {
            previewData.value = null;
            ElMessage.error(error.message || 'Protection preview failed');
        } finally {
            previewLoading.value = false;
        }
    };

    const openSelectRaw = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        previewKind.value = 'select_raw';
        previewTitle.value = 'Pick RAW';
        previewData.value = null;
        previewVisible.value = true;
        previewLoading.value = true;
        try {
            previewData.value = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/select-raw/preview`,
                {path: ctx.path}
            );
        } catch (error) {
            previewData.value = null;
            ElMessage.error(error.message || 'RAW preview failed');
        } finally {
            previewLoading.value = false;
        }
    };

    const loadPhotoImportSourceRoot = async () => {
        const response = await fetch(`/api/site-config/${encodeURIComponent(photoImportSourceRootConfigKey)}`);
        if (response.status === 404) return '';
        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error(`Source Root config returned a non-JSON response (${response.status})`);
        }
        if (!response.ok) throw new Error(data.error || `Source Root config failed (${response.status})`);
        return String(data.value || '').trim();
    };

    const savePhotoImportSourceRoot = async () => {
        const root = String(photoImportForm.value.source_root || '').trim();
        await fetchJson('/api/site-config', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({[photoImportSourceRootConfigKey]: root})
        });
    };

    const schedulePhotoImportSourceRootSave = () => {
        if (photoImportSourceRootSaveTimer) clearTimeout(photoImportSourceRootSaveTimer);
        photoImportSourceRootSaveTimer = setTimeout(async () => {
            photoImportSourceRootSaveTimer = null;
            try {
                await savePhotoImportSourceRoot();
            } catch (error) {
                ElMessage.error(error.message || 'Source Root save failed');
            }
        }, 350);
    };

    const previewPhotoImport = async () => {
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        const root = String(photoImportForm.value.source_root || '').trim();
        const gap = Number(photoImportForm.value.gap_minutes);
        photoImportError.value = '';
        if (!root) {
            photoImportError.value = 'Source Root is required';
            ElMessage.error(photoImportError.value);
            return;
        }
        if (!Number.isInteger(gap) || gap < 1 || gap > 1440) {
            photoImportError.value = 'Gap must be 1–1440 minutes';
            ElMessage.error(photoImportError.value);
            return;
        }

        photoImportLoading.value = true;
        photoImportPlan.value = null;
        photoImportSelectedGroups.value = [];
        try {
            const data = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/photo-import/preview`,
                {path: ctx.path, source_root: root, gap_minutes: gap}
            );
            photoImportPlan.value = data;
            photoImportSelectedGroups.value = (data.groups || []).map(group => group.id);
        } catch (error) {
            photoImportError.value = error.message || 'Import preview failed';
            ElMessage.error(photoImportError.value);
        } finally {
            photoImportLoading.value = false;
        }
    };

    const openPhotoImport = async () => {
        try {
            context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }
        photoImportForm.value = {
            source_root: '',
            gap_minutes: 30
        };
        photoImportPlan.value = null;
        photoImportError.value = '';
        photoImportSelectedGroups.value = [];
        photoImportVisible.value = true;
        try {
            photoImportForm.value.source_root = await loadPhotoImportSourceRoot();
        } catch (error) {
            photoImportError.value = error.message || 'Source Root load failed';
            ElMessage.error(photoImportError.value);
            return;
        }
        if (photoImportForm.value.source_root) await previewPhotoImport();
    };

    const photoImportThumbnailUrl = (fileId) => {
        const source = options.getSource && options.getSource();
        const path = options.getSetPath && options.getSetPath();
        const planId = photoImportPlan.value && photoImportPlan.value.plan_id;
        if (!source || !source.id || !planId || path === null || path === undefined) return '';
        return `/api/library/workflow/sources/${source.id}/photo-import/thumbnail/${encodeURIComponent(planId)}/${encodeURIComponent(fileId)}?path=${encodeURIComponent(path)}`;
    };

    const photoImportGroupTime = (group) => {
        if (!group) return '';
        if (group.unknown_time) return 'Unknown Time';
        if (group.start_time && group.end_time) return `${group.start_time}–${group.end_time}`;
        return group.start_time || group.end_time || '';
    };

    const executePhotoImport = async () => {
        if (!photoImportPlan.value || !photoImportPlan.value.plan_id) return;
        if (!photoImportSelectedGroups.value.length) {
            ElMessage.warning('Select at least one group');
            return;
        }
        let ctx;
        try {
            ctx = context();
        } catch (error) {
            ElMessage.error(error.message);
            return;
        }

        photoImportExecuting.value = true;
        try {
            const prepared = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/photo-import/prepare`,
                {
                    path: ctx.path,
                    plan_id: photoImportPlan.value.plan_id,
                    group_ids: photoImportSelectedGroups.value
                }
            );

            await ElMessageBox.confirm(
                `Move ${prepared.total_files} files to Original?`,
                'Import Photos',
                {
                    confirmButtonText: 'Move',
                    cancelButtonText: 'Cancel',
                    type: 'warning'
                }
            );

            const started = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/photo-import/start`,
                {path: ctx.path, plan_id: prepared.plan_id}
            );
            photoImportVisible.value = false;
            refreshedTaskId = '';
            task.value = {
                id: started.task_id,
                kind: 'photo_import',
                status: 'queued',
                total: prepared.total_files || 0,
                completed: 0,
                current: 0,
                percent: 0,
                message: 'Import queued…',
                logs: [],
                result: null,
                error: null
            };
            progressVisible.value = true;
            startPolling();
        } catch (error) {
            if (error !== 'cancel' && error !== 'close') {
                ElMessage.error(error?.message || 'Import failed');
            }
        } finally {
            photoImportExecuting.value = false;
        }
    };

    const canExecute = computed(() => {
        const data = previewData.value;
        if (!data || !data.plan_id) return false;
        if (previewKind.value === 'visual_rename') return (data.summary?.rename_count || 0) > 0;
        if (previewKind.value === 'discard_unreturned_base') {
            return (data.summary?.move_count || 0) > 0 && (data.summary?.conflict_count || 0) === 0;
        }
        if (previewKind.value === 'sync_originals') return (data.summary?.trash_count || 0) > 0;
        if (previewKind.value === 'select_raw') return (data.summary?.copy_count || 0) > 0;
        if (previewKind.value === 'protect_originals') return (data.summary?.candidate_count || 0) > 0;
        return false;
    });

    const confirmationText = (action = 'execute') => {
        const data = previewData.value || {};
        if (previewKind.value === 'visual_rename') {
            return `Rename ${data.summary?.rename_count || 0} Model Edit files?`;
        }
        if (previewKind.value === 'discard_unreturned_base') {
            return `Move ${data.summary?.move_count || 0} unmatched Base Edit files to discards?`;
        }
        if (previewKind.value === 'sync_originals') {
            return `Trash ${data.summary?.trash_count || 0} unmatched files?`;
        }
        if (previewKind.value === 'protect_originals') {
            if (action === 'unprotect') {
                return `Remove protection from ${data.summary?.protected_original_count || 0} Original files?`;
            }
            return `Protect ${data.summary?.candidate_count || 0} Original files?`;
        }
        return `Copy ${data.summary?.copy_count || 0} RAW files to Selects?`;
    };

    const startCurrent = async (action = 'execute') => {
        const unprotectOriginals = previewKind.value === 'protect_originals' && action === 'unprotect';
        if (unprotectOriginals) {
            if ((previewData.value?.summary?.protected_original_count || 0) <= 0) return;
        } else if (!canExecute.value) {
            return;
        }

        let ctx;
        try {
            ctx = context();
            await ElMessageBox.confirm(
                confirmationText(action),
                unprotectOriginals ? 'Unprotect Originals' : 'Confirm',
                {
                    confirmButtonText: unprotectOriginals ? 'Unprotect All' : 'Execute',
                    cancelButtonText: 'Cancel',
                    type: unprotectOriginals || previewKind.value === 'sync_originals' ? 'warning' : 'info'
                }
            );
        } catch (error) {
            if (error !== 'cancel' && error !== 'close' && error?.message) ElMessage.error(error.message);
            return;
        }

        let endpoint = '';
        if (previewKind.value === 'visual_rename') endpoint = 'visual-rename/start';
        if (previewKind.value === 'discard_unreturned_base') endpoint = 'discard-unreturned/start';
        if (previewKind.value === 'sync_originals') endpoint = 'sync/start';
        if (previewKind.value === 'select_raw') endpoint = 'select-raw/start';
        if (previewKind.value === 'protect_originals') endpoint = 'protect-originals/start';

        try {
            const payload = {path: ctx.path, plan_id: previewData.value.plan_id};
            if (previewKind.value === 'protect_originals') {
                payload.action = unprotectOriginals ? 'unprotect' : 'protect';
            }
            const data = await postJson(
                `/api/library/workflow/sources/${ctx.sourceId}/${endpoint}`,
                payload
            );
            previewVisible.value = false;
            refreshedTaskId = '';
            task.value = {
                id: data.task_id,
                kind: previewKind.value,
                status: 'queued',
                total: 0,
                completed: 0,
                current: 0,
                percent: 0,
                message: 'Queued…',
                logs: [],
                result: null,
                error: null
            };
            progressVisible.value = true;
            startPolling();
        } catch (error) {
            ElMessage.error(error.message || 'Failed to start task');
        }
    };

    const pollTask = async () => {
        if (!task.value.id) return;
        try {
            const data = await fetchJson(`/api/library/workflow/tasks/${encodeURIComponent(task.value.id)}`);
            task.value = data;
            if (data.status === 'done' || data.status === 'error') {
                stopPolling();
                if (data.status === 'error') {
                    ElMessage.error(data.error || 'Task failed');
                } else if (data.result?.trash_error) {
                    ElMessage.warning('Trash failed. Files remain in Deleted.');
                } else {
                    ElMessage.success(data.message || 'Done.');
                }
                if (data.status === 'done' && data.kind === 'protect_originals') {
                    await refreshProtectOriginalsStatus();
                }
                if (refreshedTaskId !== data.id && options.refreshCurrent) {
                    refreshedTaskId = data.id;
                    try {
                        await options.refreshCurrent();
                    } catch (error) {
                        console.warn('Failed to refresh folder', error);
                    }
                }
            }
        } catch (error) {
            stopPolling();
            task.value = {...task.value, status: 'error', error: error.message, message: 'Failed to load task progress'};
            ElMessage.error(error.message || 'Failed to load task progress');
        }
    };

    const startPolling = () => {
        stopPolling();
        void pollTask();
        pollTimer = window.setInterval(() => void pollTask(), 450);
    };

    const stopPolling = () => {
        if (pollTimer) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
    };

    const closeProgress = () => {
        if (task.value.status === 'running' || task.value.status === 'queued') return;
        progressVisible.value = false;
    };

    const statusLabel = (status) => ({
        rename: 'Rename',
        already_named: 'Already named',
        below_threshold: 'Below threshold',
        copy: 'Copy',
        already_exists: 'Exists',
        missing_raw: 'Missing RAW',
        ambiguous_raw: 'Ambiguous RAW',
        move_to_discards: 'Move',
        destination_exists: 'Conflict',
        move_to_deleted: 'Move',
        already_in_deleted: 'In Deleted',
        protect: 'Protect',
        protected: 'Protected'
    }[status] || status || '—');

    const statusType = (status) => ({
        rename: 'success',
        already_named: 'info',
        below_threshold: 'warning',
        copy: 'success',
        already_exists: 'info',
        missing_raw: 'danger',
        ambiguous_raw: 'warning',
        move_to_discards: 'warning',
        destination_exists: 'danger',
        move_to_deleted: 'warning',
        already_in_deleted: 'info',
        protect: 'warning',
        protected: 'success'
    }[status] || 'info');

    const formatSize = (bytes) => {
        const value = Number(bytes || 0);
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
        if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
        return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
    };

    return {
        previewVisible,
        previewLoading,
        previewKind,
        previewTitle,
        previewData,
        threshold,
        protectOriginalsLabel,
        handleWorkflowMenuVisible,
        inspectionVisible,
        inspectionRulesVisible,
        inspectionLoading,
        inspectionData,
        inspectionError,
        inspectionMetadataVisible,
        inspectionMetadataLoading,
        inspectionMetadataData,
        inspectionMetadataError,
        photoImportVisible,
        photoImportLoading,
        photoImportExecuting,
        photoImportForm,
        photoImportPlan,
        photoImportError,
        photoImportSelectedGroups,
        previewPhotoImport,
        openPhotoImport,
        executePhotoImport,
        photoImportThumbnailUrl,
        photoImportGroupTime,
        schedulePhotoImportSourceRootSave,
        progressVisible,
        task,
        canExecute,
        openImageInspection,
        openInspectionMetadata,
        openVisualRename,
        analyzeVisualRename,
        openDiscardUnreturnedBase,
        openProtectOriginals,
        openSyncRawByJpg: () => openSync('raw_by_jpg'),
        openSyncJpgByRaw: () => openSync('jpg_by_raw'),
        openSelectRaw,
        startCurrent,
        closeProgress,
        statusLabel,
        statusType,
        formatSize
    };
}

export {createController};
