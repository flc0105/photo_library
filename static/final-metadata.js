(function () {
    const {ref, computed} = Vue;
    const {ElMessage, ElMessageBox} = ElementPlus;

    function createController(options) {
        const visible = ref(false);
        const loading = ref(false);
        const step = ref(0);
        const plan = ref(null);
        const task = ref({
            id: '', status: '', total: 0, completed: 0, current: 0,
            percent: 0, message: '', logs: [], result: null, error: null
        });
        const settingsVisible = ref(false);
        const selectedRowIds = ref([]);
        const settingsSaving = ref(false);
        const settingsDraft = ref({selected_fields: [], additional_tags: []});
        const settingsFields = ref([]);
        const additionalTagsText = ref('');
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

        const loadPlan = async () => {
            const ctx = context();
            loading.value = true;
            try {
                plan.value = await postJson(
                    `/api/library/final-metadata/sources/${ctx.sourceId}/plan`,
                    {path: ctx.path}
                );
                settingsFields.value = plan.value.fields || [];
                selectedRowIds.value = (plan.value.rows || [])
                    .filter(row => row.default_selected)
                    .map(row => row.id);
            } finally {
                loading.value = false;
            }
        };

        const open = async () => {
            stopPolling();
            try {
                context();
            } catch (error) {
                ElMessage.error(error.message);
                return;
            }
            visible.value = true;
            step.value = 0;
            plan.value = null;
            refreshedTaskId = '';
            resetTask();
            try {
                await loadPlan();
            } catch (error) {
                ElMessage.error(error.message || '读取 Final Metadata Plan 失败');
                visible.value = false;
            }
        };

        const refreshPlan = async () => {
            try {
                await loadPlan();
            } catch (error) {
                ElMessage.error(error.message || '刷新 Metadata Plan 失败');
            }
        };

        const thumbnailUrl = (row, side) => {
            let ctx;
            try {
                ctx = context();
            } catch (error) {
                return '';
            }
            if (!plan.value || !plan.value.plan_id || !row || !row.id) return '';
            return `/api/library/final-metadata/sources/${ctx.sourceId}/thumbnail/${encodeURIComponent(plan.value.plan_id)}/${encodeURIComponent(row.id)}/${encodeURIComponent(side)}`;
        };

        const isRowSelected = (row) => selectedRowIds.value.includes(row.id);

        const setRowSelected = (row, checked) => {
            if (!row || row.status === 'blocked') return;
            const next = new Set(selectedRowIds.value);
            if (checked) next.add(row.id);
            else next.delete(row.id);
            selectedRowIds.value = [...next];
        };

        const selectableRows = computed(() => (plan.value && plan.value.rows ? plan.value.rows : [])
            .filter(row => row.status !== 'blocked'));
        const selectedCount = computed(() => selectedRowIds.value.length);
        const allRowsSelected = computed(() => selectableRows.value.length > 0
            && selectableRows.value.every(row => selectedRowIds.value.includes(row.id)));
        const someRowsSelected = computed(() => selectedRowIds.value.length > 0 && !allRowsSelected.value);

        const setAllRowsSelected = (checked) => {
            selectedRowIds.value = checked ? selectableRows.value.map(row => row.id) : [];
        };

        const openSettings = async () => {
            loading.value = true;
            try {
                const data = await fetchJson('/api/library/final-metadata/settings');
                settingsFields.value = data.fields || [];
                settingsDraft.value = {
                    selected_fields: [...((data.settings && data.settings.selected_fields) || [])],
                    additional_tags: [...((data.settings && data.settings.additional_tags) || [])]
                };
                additionalTagsText.value = settingsDraft.value.additional_tags.join(', ');
                settingsVisible.value = true;
            } catch (error) {
                ElMessage.error(error.message || '读取 Metadata 设置失败');
            } finally {
                loading.value = false;
            }
        };

        const saveSettings = async () => {
            settingsSaving.value = true;
            try {
                const extra = additionalTagsText.value
                    .split(/[,\n]+/)
                    .map(item => item.trim())
                    .filter(Boolean);
                const data = await postJson('/api/library/final-metadata/settings', {
                    settings: {
                        selected_fields: settingsDraft.value.selected_fields,
                        additional_tags: extra
                    }
                });
                settingsDraft.value = {
                    selected_fields: [...((data.settings && data.settings.selected_fields) || [])],
                    additional_tags: [...((data.settings && data.settings.additional_tags) || [])]
                };
                settingsVisible.value = false;
                ElMessage.success('Final Metadata 字段设置已保存');
                await loadPlan();
            } catch (error) {
                ElMessage.error(error.message || '保存 Metadata 设置失败');
            } finally {
                settingsSaving.value = false;
            }
        };

        const pollTask = async () => {
            if (!task.value.id) return;
            try {
                const data = await fetchJson(`/api/library/final-metadata/tasks/${encodeURIComponent(task.value.id)}`);
                task.value = data;
                if (data.status === 'running' || data.status === 'queued') {
                    pollTimer = setTimeout(pollTask, 650);
                    return;
                }
                if (data.status === 'done') {
                    ElMessage.success(data.message || 'Final Metadata 完成');
                    if (refreshedTaskId !== data.id && options.refreshCurrent) {
                        refreshedTaskId = data.id;
                        await options.refreshCurrent();
                    }
                } else if (data.status === 'error') {
                    ElMessage.error(data.error || 'Final Metadata 失败');
                }
            } catch (error) {
                task.value = {...task.value, status: 'error', error: error.message};
                ElMessage.error(error.message || '读取 Final Metadata 进度失败');
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
                    `将重建 ${selectedRowIds.value.length} 张已选择的 05_Final JPEG metadata：使用 -all= 清空来源 metadata，但明确保留现有 JFIF 与 ICC，并删除 Adobe APP14；随后优先从 Original/JPG（缺失时 Base Edit）写入当前白名单字段。JFIF、ICC、JPEG 图像数据和解码后的显示像素都必须保持不变，否则整批拒绝发布。`,
                    'Write Final Metadata',
                    {confirmButtonText: '开始写入', cancelButtonText: '取消', type: 'warning'}
                );
            } catch (error) {
                return;
            }

            loading.value = true;
            try {
                const data = await postJson(
                    `/api/library/final-metadata/sources/${ctx.sourceId}/start`,
                    {path: ctx.path, plan_id: plan.value.plan_id, row_ids: selectedRowIds.value}
                );
                resetTask();
                task.value.id = data.task_id;
                task.value.status = 'queued';
                task.value.message = '等待开始…';
                task.value.total = selectedRowIds.value.length;
                step.value = 1;
                stopPolling();
                pollTimer = setTimeout(pollTask, 150);
            } catch (error) {
                ElMessage.error(error.message || '启动 Final Metadata 失败');
            } finally {
                loading.value = false;
            }
        };

        const close = () => {
            if (task.value.status === 'running' || task.value.status === 'queued') return;
            stopPolling();
            visible.value = false;
        };

        const canExecute = computed(() => !!(
            plan.value
            && plan.value.can_execute
            && selectedRowIds.value.length > 0
            && selectedRowIds.value.every(rowId => {
                const row = (plan.value.rows || []).find(item => item.id === rowId);
                return row && row.status !== 'blocked';
            })
            && !loading.value
        ));

        return {
            visible, loading, step, plan, task, canExecute,
            selectedRowIds, selectedCount, allRowsSelected, someRowsSelected,
            isRowSelected, setRowSelected, setAllRowsSelected,
            settingsVisible, settingsSaving, settingsDraft, settingsFields, additionalTagsText,
            open, close, refreshPlan, thumbnailUrl, openSettings, saveSettings, execute
        };
    }

    window.FinalMetadata = {createController};
})();
