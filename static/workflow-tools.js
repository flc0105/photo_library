(function () {
    const {ref, computed} = Vue;
    const {ElMessage, ElMessageBox} = ElementPlus;

    function createController(options) {
        const previewVisible = ref(false);
        const previewLoading = ref(false);
        const previewKind = ref('');
        const previewTitle = ref('Advanced Feature');
        const previewData = ref(null);
        const threshold = ref(0.8);

        const inspectionVisible = ref(false);
        const inspectionLoading = ref(false);
        const inspectionData = ref(null);
        const inspectionError = ref('');

        const photoImportVisible = ref(false);
        const photoImportLoading = ref(false);
        const photoImportExecuting = ref(false);
        const photoImportForm = ref({
            source_root: '/Users/flc/Pictures/Camera Exports/',
            gap_minutes: 30
        });
        const photoImportPlan = ref(null);
        const photoImportSelectedGroups = ref([]);

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
                throw new Error('当前没有可操作的 Set');
            }
            return {sourceId: source.id, path};
        };

        const fetchJson = async (url, init) => {
            const response = await fetch(url, init);
            let data = null;
            try {
                data = await response.json();
            } catch (error) {
                throw new Error(`服务器返回了非 JSON 响应 (${response.status})`);
            }
            if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
            return data;
        };

        const postJson = (url, payload) => fetchJson(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload || {})
        });

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
                inspectionError.value = error.message || '图像检测失败';
                ElMessage.error(inspectionError.value);
            } finally {
                inspectionLoading.value = false;
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
                ElMessage.error('相似度阈值必须在 0.0–1.0 之间');
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
                ElMessage.error(error.message || 'Visual Rename 预览失败');
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
                ElMessage.error(error.message || 'Base 未返图检查失败');
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
                ElMessage.error(error.message || '同步预览失败');
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
            previewTitle.value = 'Favorite JPG → RAW Selects';
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
                ElMessage.error(error.message || 'RAW Selects 预览失败');
            } finally {
                previewLoading.value = false;
            }
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
            if (!root) {
                ElMessage.error('Source Root 不能为空');
                return;
            }
            if (!Number.isInteger(gap) || gap < 1 || gap > 1440) {
                ElMessage.error('Gap 必须是 1–1440 分钟的整数');
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
                ElMessage.error(error.message || '照片导入 Preview 失败');
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
                source_root: '/Users/flc/Pictures/Camera Exports/',
                gap_minutes: 30
            };
            photoImportPlan.value = null;
            photoImportSelectedGroups.value = [];
            photoImportVisible.value = true;
            await previewPhotoImport();
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
                ElMessage.warning('至少选择一组照片');
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
                    `将移动 ${prepared.jpg_count} 个 JPG 和 ${prepared.raw_count} 个同 stem RAW（共 ${prepared.total_files} 个文件）到当前 Set 的 01_Original/JPG 与 01_Original/RAW。执行后源目录中的这些文件会被移走。`,
                    '确认导入',
                    {
                        confirmButtonText: 'Move',
                        cancelButtonText: '取消',
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
                    message: '导入任务已提交…',
                    logs: [],
                    result: null,
                    error: null
                };
                progressVisible.value = true;
                startPolling();
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') {
                    ElMessage.error(error?.message || '导入失败');
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
            return false;
        });

        const confirmationText = () => {
            const data = previewData.value || {};
            if (previewKind.value === 'visual_rename') {
                return `将按当前预览重命名 ${data.summary?.rename_count || 0} 个 03_Model_Edit 文件。执行前已固定映射，文件发生变化时会拒绝执行。`;
            }
            if (previewKind.value === 'discard_unreturned_base') {
                return `将把 ${data.summary?.move_count || 0} 个在 03_Model_Edit 中没有同 stem 的 Base_Edit 文件移动到 02_Base_Edit/discards。只移动，不删除；预览后 Base/Model 文件发生变化时会拒绝执行。`;
            }
            if (previewKind.value === 'sync_originals') {
                return `请再次确认你已经检查过预览列表。最终将有 ${data.summary?.trash_count || 0} 个文件随 Deleted 进入系统回收站，其中 ${data.summary?.move_count || 0} 个会先从当前目录移动进 Deleted。不会调用永久删除。`;
            }
            return `将把 ${data.summary?.copy_count || 0} 个收藏 JPG 对应的 CR3 复制到 01_Original/Selects。已有同名 RAW 不会覆盖。`;
        };

        const startCurrent = async () => {
            if (!canExecute.value) return;
            let ctx;
            try {
                ctx = context();
                await ElMessageBox.confirm(
                    confirmationText(),
                    '确认执行',
                    {
                        confirmButtonText: '执行',
                        cancelButtonText: '取消',
                        type: previewKind.value === 'sync_originals' ? 'warning' : 'info'
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

            try {
                const data = await postJson(
                    `/api/library/workflow/sources/${ctx.sourceId}/${endpoint}`,
                    {path: ctx.path, plan_id: previewData.value.plan_id}
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
                    message: '任务已提交…',
                    logs: [],
                    result: null,
                    error: null
                };
                progressVisible.value = true;
                startPolling();
            } catch (error) {
                ElMessage.error(error.message || '启动任务失败');
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
                        ElMessage.error(data.error || '任务失败');
                    } else if (data.result?.trash_error) {
                        ElMessage.warning('文件已安全留在 Deleted；系统回收站调用失败，请查看进度日志。');
                    } else {
                        ElMessage.success(data.message || '操作完成');
                    }
                    if (refreshedTaskId !== data.id && options.refreshCurrent) {
                        refreshedTaskId = data.id;
                        try {
                            await options.refreshCurrent();
                        } catch (error) {
                            console.warn('刷新当前目录失败', error);
                        }
                    }
                }
            } catch (error) {
                stopPolling();
                task.value = {...task.value, status: 'error', error: error.message, message: '读取任务进度失败'};
                ElMessage.error(error.message || '读取任务进度失败');
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
            already_in_deleted: 'In Deleted'
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
            already_in_deleted: 'info'
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
            inspectionVisible,
            inspectionLoading,
            inspectionData,
            inspectionError,
            photoImportVisible,
            photoImportLoading,
            photoImportExecuting,
            photoImportForm,
            photoImportPlan,
            photoImportSelectedGroups,
            previewPhotoImport,
            openPhotoImport,
            executePhotoImport,
            photoImportThumbnailUrl,
            photoImportGroupTime,
            progressVisible,
            task,
            canExecute,
            openImageInspection,
            openVisualRename,
            analyzeVisualRename,
            openDiscardUnreturnedBase,
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

    window.WorkflowTools = {createController};
})();
