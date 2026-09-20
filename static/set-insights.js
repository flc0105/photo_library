(() => {
    const {ref, computed} = Vue;

    function createController(options) {
        const setStats = ref(null);
        const statsLoading = ref(false);
        const statsError = ref('');

        const equipment = ref(null);
        const equipmentState = ref('idle'); // idle | scanning | ready | error
        const equipmentError = ref('');

        const validationVisible = ref(false);
        const validationLoading = ref(false);
        const validationData = ref(null);
        const validationError = ref('');

        let detailRequestToken = 0;

        const fetchJson = async (url, init = undefined) => {
            const response = await fetch(url, init);
            let data;
            try {
                data = await response.json();
            } catch (error) {
                throw new Error(`服务器返回了非 JSON 响应 (${response.status})`);
            }
            if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
            return data;
        };

        const currentContext = () => {
            const source = options.getSource && options.getSource();
            const path = options.getSetPath && options.getSetPath();
            if (!source || !source.id) throw new Error('当前 Library Source 不可用');
            return {sourceId: source.id, path: path || ''};
        };

        const formatFocal = (value) => {
            const number = Number(value);
            if (!Number.isFinite(number)) return String(value ?? '');
            return Number.isInteger(number) ? String(number) : number.toFixed(1).replace(/\.0$/, '');
        };

        const focalText = computed(() => {
            const values = Array.isArray(equipment.value?.focal_lengths) ? equipment.value.focal_lengths : [];
            return values.length ? `${values.map(formatFocal).join(' · ')} mm` : '—';
        });

        const cameraText = computed(() => {
            const values = Array.isArray(equipment.value?.cameras) ? equipment.value.cameras : [];
            return values.length ? values.join(' · ') : '—';
        });

        const lensText = computed(() => {
            const values = Array.isArray(equipment.value?.lenses) ? equipment.value.lenses : [];
            return values.length ? values.join(' · ') : '—';
        });

        const loadStats = async (token, ctx) => {
            statsLoading.value = true;
            statsError.value = '';
            try {
                const data = await fetchJson(
                    `/api/library/insights/sources/${ctx.sourceId}/set-stats?path=${encodeURIComponent(ctx.path)}`
                );
                if (token !== detailRequestToken) return;
                setStats.value = data;
            } catch (error) {
                if (token !== detailRequestToken) return;
                setStats.value = null;
                statsError.value = error.message || '统计失败';
            } finally {
                if (token === detailRequestToken) statsLoading.value = false;
            }
        };

        const loadEquipment = async (token, ctx, force = false) => {
            equipmentState.value = 'scanning';
            equipmentError.value = '';
            if (force) equipment.value = null;
            try {
                const data = await fetchJson(
                    `/api/library/insights/sources/${ctx.sourceId}/equipment?path=${encodeURIComponent(ctx.path)}${force ? '&force=1' : ''}`
                );
                if (token !== detailRequestToken) return;
                equipment.value = data;
                equipmentState.value = 'ready';
            } catch (error) {
                if (token !== detailRequestToken) return;
                equipment.value = null;
                equipmentState.value = 'error';
                equipmentError.value = error.message || '设备信息扫描失败';
            }
        };

        const loadDetail = async () => {
            let ctx;
            try {
                ctx = currentContext();
            } catch (error) {
                return;
            }
            const token = ++detailRequestToken;
            setStats.value = null;
            statsError.value = '';
            equipment.value = null;
            equipmentState.value = 'scanning';
            equipmentError.value = '';

            // Counts are cheap and equipment metadata can be slower. Start both only
            // after the Detail dialog is already visible; neither blocks the dialog.
            void loadStats(token, ctx);
            window.setTimeout(() => {
                if (token === detailRequestToken) void loadEquipment(token, ctx, false);
            }, 0);
        };

        const rescanEquipment = async () => {
            let ctx;
            try {
                ctx = currentContext();
            } catch (error) {
                ElMessage.error(error.message);
                return;
            }
            const token = ++detailRequestToken;
            await loadEquipment(token, ctx, true);
        };

        const openValidation = async () => {
            const source = options.getSource && options.getSource();
            if (!source || !source.id) {
                ElMessage.error('当前 Library Source 不可用');
                return;
            }
            validationVisible.value = true;
            validationLoading.value = true;
            validationData.value = null;
            validationError.value = '';
            try {
                validationData.value = await fetchJson(
                    `/api/library/insights/sources/${source.id}/validate-root`,
                    {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}
                );
            } catch (error) {
                validationError.value = error.message || 'Validation 失败';
                ElMessage.error(validationError.value);
            } finally {
                validationLoading.value = false;
            }
        };

        const versionText = (versions) => {
            if (!Array.isArray(versions) || !versions.length) return '—';
            return versions.map(item => `${item.name} ×${item.count}`).join(' · ');
        };

        return {
            setStats,
            statsLoading,
            statsError,
            equipment,
            equipmentState,
            equipmentError,
            cameraText,
            lensText,
            focalText,
            validationVisible,
            validationLoading,
            validationData,
            validationError,
            loadDetail,
            rescanEquipment,
            openValidation,
            versionText,
        };
    }

    window.SetInsights = {createController};
})();
