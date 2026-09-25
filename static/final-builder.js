(function () {
    const {ref, computed} = Vue;
    const {ElMessage, ElMessageBox} = ElementPlus;

    function createController(options) {
        const visible = ref(false);
        const loading = ref(false);
        const step = ref(0);
        const selection = ref(null);
        const selectedIds = ref([]);
        const plan = ref(null);
        const task = ref({
            id: '', status: '', total: 0, completed: 0, current: 0,
            percent: 0, message: '', logs: [], result: null, error: null
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

        const resetTask = () => {
            task.value = {
                id: '', status: '', total: 0, completed: 0, current: 0,
                percent: 0, message: '', logs: [], result: null, error: null
            };
        };

        const stopPolling = () => {
            if (pollTimer) {
                clearTimeout(pollTimer);
                pollTimer = null;
            }
        };

        const open = async () => {
            stopPolling();
            let ctx;
            try {
                ctx = context();
            } catch (error) {
                ElMessage.error(error.message);
                return;
            }
            visible.value = true;
            loading.value = true;
            step.value = 0;
            selection.value = null;
            selectedIds.value = [];
            plan.value = null;
            refreshedTaskId = '';
            resetTask();
            try {
                const data = await postJson(
                    `/api/library/final-builder/sources/${ctx.sourceId}/selection`,
                    {path: ctx.path}
                );
                selection.value = data;
                const defaults = [];
                (data.groups || []).forEach(group => {
                    (group.items || []).forEach(item => {
                        if (item.default_selected) defaults.push(item.id);
                    });
                });
                selectedIds.value = defaults;
            } catch (error) {
                ElMessage.error(error.message || '读取 Final 候选失败');
                visible.value = false;
            } finally {
                loading.value = false;
            }
        };

        const isSelected = itemId => selectedIds.value.includes(itemId);

        const setSelected = (itemId, checked) => {
            const current = new Set(selectedIds.value);
            if (checked) current.add(itemId);
            else current.delete(itemId);
            selectedIds.value = Array.from(current);
        };

        const selectGroup = (group, checked) => {
            const current = new Set(selectedIds.value);
            (group.items || []).forEach(item => {
                if (checked) current.add(item.id);
                else current.delete(item.id);
            });
            selectedIds.value = Array.from(current);
        };

        const thumbnailUrl = item => {
            let ctx;
            try {
                ctx = context();
            } catch (error) {
                return '';
            }
            if (!selection.value || !selection.value.plan_id || !item || !item.id) return '';
            return `/api/library/final-builder/sources/${ctx.sourceId}/thumbnail/${encodeURIComponent(selection.value.plan_id)}/${encodeURIComponent(item.id)}`;
        };

        const preview = async () => {
            if (!selection.value || !selection.value.plan_id) return;
            if (!selectedIds.value.length) {
                ElMessage.warning('至少选择一张图片');
                return;
            }
            let ctx;
            try {
                ctx = context();
            } catch (error) {
                ElMessage.error(error.message);
                return;
            }
            loading.value = true;
            try {
                plan.value = await postJson(
                    `/api/library/final-builder/sources/${ctx.sourceId}/plan`,
                    {
                        path: ctx.path,
                        selection_plan_id: selection.value.plan_id,
                        selected_ids: selectedIds.value
                    }
                );
                step.value = 1;
            } catch (error) {
                ElMessage.error(error.message || '生成 Build Plan 失败');
            } finally {
                loading.value = false;
            }
        };

        const backToSelection = () => {
            if (task.value.status === 'running' || task.value.status === 'queued') return;
            step.value = 0;
            plan.value = null;
        };

        const pollTask = async () => {
            if (!task.value.id) return;
            try {
                const data = await fetchJson(`/api/library/final-builder/tasks/${encodeURIComponent(task.value.id)}`);
                task.value = data;
                if (data.status === 'running' || data.status === 'queued') {
                    pollTimer = setTimeout(pollTask, 650);
                    return;
                }
                if (data.status === 'done') {
                    ElMessage.success(data.message || 'Build Final 完成');
                    if (refreshedTaskId !== data.id && options.refreshCurrent) {
                        refreshedTaskId = data.id;
                        await options.refreshCurrent();
                    }
                } else if (data.status === 'error') {
                    ElMessage.error(data.error || 'Build Final 失败');
                }
            } catch (error) {
                task.value = {...task.value, status: 'error', error: error.message};
                ElMessage.error(error.message || '读取 Build Final 进度失败');
            }
        };

        const execute = async () => {
            if (!plan.value || !plan.value.plan_id || !plan.value.can_execute) return;
            let ctx;
            try {
                ctx = context();
            } catch (error) {
                ElMessage.error(error.message);
                return;
            }
            try {
                await ElMessageBox.confirm(
                    `将生成 ${plan.value.summary.selected_count} 张 JPEG 到 05_Final。源文件只读，不会被修改；已有同名 Final 不会覆盖。`,
                    'Build Final',
                    {confirmButtonText: '开始生成', cancelButtonText: '取消', type: 'warning'}
                );
            } catch (error) {
                return;
            }

            loading.value = true;
            try {
                const data = await postJson(
                    `/api/library/final-builder/sources/${ctx.sourceId}/start`,
                    {path: ctx.path, plan_id: plan.value.plan_id}
                );
                resetTask();
                task.value.id = data.task_id;
                task.value.status = 'queued';
                task.value.message = '等待开始…';
                task.value.total = plan.value.summary.selected_count;
                step.value = 2;
                stopPolling();
                pollTimer = setTimeout(pollTask, 150);
            } catch (error) {
                ElMessage.error(error.message || '启动 Build Final 失败');
            } finally {
                loading.value = false;
            }
        };

        const close = () => {
            if (task.value.status === 'running' || task.value.status === 'queued') return;
            stopPolling();
            visible.value = false;
        };

        const continueToMetadata = async () => {
            if (task.value.status !== 'done' || !options.openMetadata) return;
            stopPolling();
            step.value = 3;
            visible.value = false;
            await options.openMetadata();
        };

        const selectedCount = computed(() => selectedIds.value.length);
        const canPreview = computed(() => !loading.value && selectedIds.value.length > 0);
        const canExecute = computed(() => !!(plan.value && plan.value.can_execute && !loading.value));

        const statusType = status => {
            if (status === 'ready') return 'success';
            if (status === 'warning') return 'warning';
            return 'danger';
        };

        const statusText = status => {
            if (status === 'ready') return 'Ready';
            if (status === 'warning') return 'Warning';
            return 'Blocked';
        };

        return {
            visible, loading, step, selection, selectedIds, selectedCount, plan, task,
            canPreview, canExecute, open, close, continueToMetadata, isSelected, setSelected, selectGroup,
            thumbnailUrl, preview, backToSelection, execute, statusType, statusText
        };
    }

    window.FinalBuilder = {createController};
})();
