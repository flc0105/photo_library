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
            previewTitle.value = 'Visual Rename · Model Edit';
            threshold.value = 0.8;
            previewData.value = null;
            previewVisible.value = true;
            await analyzeVisualRename();
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
                ? 'Sync RAW · 以 JPG 为准'
                : 'Sync JPG · 以 RAW 为准';
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

        const canExecute = computed(() => {
            const data = previewData.value;
            if (!data || !data.plan_id) return false;
            if (previewKind.value === 'visual_rename') return (data.summary?.rename_count || 0) > 0;
            if (previewKind.value === 'sync_originals') return (data.summary?.trash_count || 0) > 0;
            if (previewKind.value === 'select_raw') return (data.summary?.copy_count || 0) > 0;
            return false;
        });

        const confirmationText = () => {
            const data = previewData.value || {};
            if (previewKind.value === 'visual_rename') {
                return `将按当前预览重命名 ${data.summary?.rename_count || 0} 个 03_Model_Edit 文件。执行前已固定映射，文件发生变化时会拒绝执行。`;
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
            progressVisible,
            task,
            canExecute,
            openVisualRename,
            analyzeVisualRename,
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
