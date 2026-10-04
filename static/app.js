const {createApp, ref, onMounted, onUnmounted, nextTick, computed, watch} = Vue;
const {ElMessage, ElMessageBox} = ElementPlus;

const app = createApp({
    setup() {
        const currentView = ref('albums');
        const albums = ref([]);
        const images = ref([]);
        const currentAlbum = ref({});
        const currentImage = ref({});

        const showCreateAlbumDialog = ref(false);
        const showEditAlbumDialog = ref(false);
        const showUploadDialog = ref(false);

        const SMART_ALBUM_DEFAULT_CODE = `SOURCE = "2026"
result = [
    photo
    for photo in photos
    if photo.source.name == SOURCE
    and photo.state.favorite
    and photo.stage == "final"
]
`;

        const newAlbum = ref({
            type: 'uploaded',
            name: '',
            description: '',
            shoot_date: '',
            model_name: '',
            location: '',
            group_ids: [], // 所属分组ID列表
        });

        // Smart Album and Smart Set share one Smart View presentation on the home page.
        // Their proven query/index backends stay separate by result type and continue to reuse
        // the same Smart Album index/runtime infrastructure underneath.
        const emptySmartAlbumIndexProgress = () => ({
            active: false,
            percent: 0,
            phase: 'idle',
            message: '',
            current: 0,
            total: 0,
            overall: {dimension: 'image', label: '图片总进度', current: 0, total: 0, percent: 0, ready: false},
            steps: [],
            summary: {
                overall_dimension: 'image',
                source_total: 0,
                source_available: 0,
                selected_source_ids: [],
                selected_source_names: [],
                set_total: 0,
                asset_total: 0,
                stage_counts: {},
                manifest_mode: 'live',
                state_mode: 'live',
                originals_indexed: false,
                capture_metadata_source: 'asset'
            },
            error: ''
        });
        const smartAlbums = ref([]);
        const currentSmartAlbum = ref({});
        const smartAlbumImages = ref([]);
        const smartAlbumLoading = ref(false);
        const smartAlbumIndexRefreshing = ref(false);
        const smartAlbumIndexProgress = ref(emptySmartAlbumIndexProgress());
        const smartAlbumIndex = ref({ready: false, asset_count: 0, set_count: 0, last_refresh_at: null, warnings: [], indexed_source_ids: [], indexed_source_names: []});
        const smartAlbumIndexSelectedSourceIds = ref([]);
        const emptySmartAlbumSyncProgress = () => ({
            active: false,
            phase: 'idle',
            message: '',
            percent: 0,
            current: 0,
            total: 0,
            steps: [],
            summary: {},
            error: '',
            plan: null,
            index: null
        });
        const smartAlbumSyncSelectedSourceIds = ref([]);
        const smartAlbumSyncScanning = ref(false);
        const smartAlbumSyncActive = ref(false);
        const smartAlbumSyncPlan = ref(null);
        const smartAlbumSyncProgress = ref(emptySmartAlbumSyncProgress());
        const showSmartAlbumSyncDialog = ref(false);
        const smartAlbumQueryError = ref('');
        const smartAlbumSort = ref({field: 'query_order', order: 'asc'});
        const smartAlbumSortOptions = [
            {label: '查询顺序', value: {field: 'query_order', order: 'asc'}},
            {label: '拍摄时间（最新）', value: {field: 'capture_time', order: 'desc'}},
            {label: '拍摄时间（最旧）', value: {field: 'capture_time', order: 'asc'}},
        ];
        const showSmartAlbumIndexDialog = ref(false);
        const showEditSmartAlbumDialog = ref(false);
        const showSmartAlbumHelpDialog = ref(false);
        const smartAlbumHelpLoading = ref(false);
        const smartAlbumRuntime = ref(null);
        const smartAlbumEditor = ref({id: null, name: '', description: '', python_code: SMART_ALBUM_DEFAULT_CODE});

        const SMART_SET_DEFAULT_CODE = `result = list(sets)
`;
        const showCreateSmartViewDialog = ref(false);
        const smartViewCreateEditor = ref({
            type: 'album',
            name: '',
            description: '',
            python_code: SMART_ALBUM_DEFAULT_CODE,
        });
        let smartViewCreatePreviousType = 'album';
        const smartSets = ref([]);
        const currentSmartSet = ref({});
        const smartSetResults = ref([]);
        // Smart Set only decides which Sets are included. Rendering reuses the existing
        // Library Set-parent listing, hydrated through the same browse API.
        const smartSetDirectoryItems = ref([]);
        // Temporary navigation context only: when a real Set is opened from a
        // Smart Set result, the Set root's parent is the originating Smart Set.
        // It is intentionally not persisted and does not affect Library browsing.
        let smartSetEntryContext = null;
        const smartSetLoading = ref(false);
        const smartSetQueryError = ref('');
        const showEditSmartSetDialog = ref(false);
        const showSmartSetHelpDialog = ref(false);
        const smartSetHelpLoading = ref(false);
        const smartSetRuntime = ref(null);
        const smartSetEditor = ref({id: null, name: '', description: '', python_code: SMART_SET_DEFAULT_CODE});
        const smartViews = computed(() => {
            const albums = smartAlbums.value.map(item => ({
                ...item,
                smart_view_type: 'album',
                smart_view_key: `album:${item.id}`,
            }));
            const sets = smartSets.value.map(item => ({
                ...item,
                smart_view_type: 'set',
                smart_view_key: `set:${item.id}`,
            }));
            return [...albums, ...sets].sort((a, b) => {
                const byCreated = String(b.created_at || '').localeCompare(String(a.created_at || ''));
                if (byCreated) return byCreated;
                return String(b.smart_view_key).localeCompare(String(a.smart_view_key));
            });
        });

        const emptyExploreStats = () => ({
            total_images: 0,
            total_sets: 0,
            sources: [],
            years: [],
            models: [],
            environments: [],
            themes: [],
            locations: [],
            focal_lengths: [],
            index: null,
        });
        const exploreStats = ref(emptyExploreStats());
        const exploreLoading = ref(false);
        const exploreQueryLoading = ref(false);
        const exploreError = ref('');
        const exploreSelectedYear = ref(null);
        const EXPLORE_COLLAPSED_ROWS = 10;
        const EXPLORE_STAT_COLUMNS = 2;
        const EXPLORE_COLLAPSED_ITEMS = EXPLORE_COLLAPSED_ROWS * EXPLORE_STAT_COLUMNS;
        const exploreExpandedSections = ref({
            months: false,
            models: false,
            environments: false,
            themes: false,
            locations: false,
            focal_lengths: false,
        });
        let exploreReturnScrollY = 0;


        const detailImageList = computed(() => filteredImages.value);

        const currentImageIndex = computed(() => {
            return detailImageList.value.findIndex(img => img.id === currentImage.value.id);
        });


        const hasPrev = computed(() => currentImageIndex.value > 0);
        const hasNext = computed(() => currentImageIndex.value < detailImageList.value.length - 1);

        const prevImage = () => {
            if (!hasPrev.value) {
                ElMessage.info('已经是第一张图片了');
                return;
            }
            const image = detailImageList.value[currentImageIndex.value - 1];
            if (image && image.source_type === 'library') {
                void viewLibraryImage(image, false);
            } else if (image && image.source_type === 'library-share') {
                void viewSharedImage(image, false);
            } else {
                currentImage.value = image;
            }
        };

        const nextImage = () => {
            if (!hasNext.value) {
                ElMessage.info('已经是最后一张图片了');
                return;
            }
            const image = detailImageList.value[currentImageIndex.value + 1];
            if (image && image.source_type === 'library') {
                void viewLibraryImage(image, false);
            } else if (image && image.source_type === 'library-share') {
                void viewSharedImage(image, false);
            } else {
                currentImage.value = image;
            }
        };

        const getAlbumImageCount = (albumId) => {
            const album = albums.value.find(a => a.id === albumId);
            return album ? album.image_count : 0;
        };


        const loadAlbums = async () => {
            try {
                const response = await fetch('/api/album-groups');
                const allData = await response.json();

                // 1. 所有分组（包含未分组）给 albumGroups
                albumGroups.value = allData;

                // 2. 过滤掉"未分组"的分组给 allGroups（用于下拉选择）
                allGroups.value = allData.filter(group => {
                    // 排除名为"未分组"或者有 is_ungrouped 标记的分组
                    return group.name !== '未分组' && !group.is_ungrouped;
                });

                console.log('所有分组:', allData.length);
                console.log('过滤后的分组（用于下拉）:', allGroups.value.length);

            } catch (error) {
                ElMessage.error('加载相册失败');
            }
        };


        const openAlbum = async (albumId) => {
            // 从所有分组中查找相册
            let foundAlbum = null;

            // 遍历所有分组查找相册
            for (const group of albumGroups.value) {
                if (group.albums && group.albums.length > 0) {
                    const album = group.albums.find(a => a.id === albumId);
                    if (album) {
                        foundAlbum = album;
                        break;
                    }
                }
            }

            if (!foundAlbum) {
                ElMessage.error('相册不存在');
                return;
            }

            // 如果有密码保护
            // 如果有密码保护且不是管理员
            if (foundAlbum && foundAlbum.has_password && !isAdmin.value) {
                const hasToken = !!checkAlbumAccess(albumId);
                if (!hasToken) {
                    const success = await showPasswordDialog(foundAlbum);
                    if (success) {
                        currentAlbum.value = {...foundAlbum};
                        const loaded = await loadAlbumImages(albumId);
                        if (loaded) {
                            currentView.value = 'album-detail';
                        }
                    }
                    return;
                }
            }

            // 如果没有密码或已有访问权限，直接打开
            // 如果没有密码、或者有密码但有访问token、或者是管理员，直接打开
            if (foundAlbum) {
                currentAlbum.value = {...foundAlbum};
                const success = await loadAlbumImages(albumId);
                if (success) {
                    currentView.value = 'album-detail';
                }
            }
        };

        const openAlbumDirect = async (albumId, targetImageId = null) => {
            // 从所有分组中查找相册
            let foundAlbum = null;

            // 遍历所有分组查找相册
            for (const group of albumGroups.value) {
                if (group.albums && group.albums.length > 0) {
                    const album = group.albums.find(a => a.id === albumId);
                    if (album) {
                        foundAlbum = album;
                        break;
                    }
                }
            }

            if (!foundAlbum) {
                ElMessage.error('相册不存在');
                return;
            }

            // 如果有密码保护
            // 如果有密码保护且不是管理员
            if (foundAlbum && foundAlbum.has_password && !isAdmin.value) {
                const hasToken = !!checkAlbumAccess(albumId);
                if (!hasToken) {
                    const success = await showPasswordDialog(foundAlbum);
                    if (success) {
                        currentAlbum.value = {...foundAlbum};
                        const loaded = await loadAlbumImages(albumId);
                        if (loaded) {
                            currentView.value = 'album-detail';

                            // 如果有指定的图片ID，直接打开该图片
                            if (targetImageId) {
                                // 在筛选后的图片中查找
                                const targetImage = filteredImages.value.find(img => img.id === targetImageId);
                                if (targetImage) {
                                    // 短暂延迟确保页面渲染完成
                                    setTimeout(() => {
                                        viewImage(targetImageId);
                                    }, 300);
                                } else {
                                    ElMessage.warning('指定的图片不存在');
                                }
                            }


                            // 去掉URL参数
                            updateUrlWithoutParams();
                        }
                    }
                    return;
                }
            }

            // 如果没有密码或已有访问权限，直接打开
            // 如果没有密码、或者有密码但有访问token、或者是管理员，直接打开
            if (foundAlbum) {
                currentAlbum.value = {...foundAlbum};
                const success = await loadAlbumImages(albumId);
                if (success) {
                    currentView.value = 'album-detail';

                    // 如果有指定的图片ID，直接打开该图片
                    if (targetImageId) {
                        // 在筛选后的图片中查找
                        const targetImage = filteredImages.value.find(img => img.id === targetImageId);
                        if (targetImage) {
                            // 短暂延迟确保页面渲染完成
                            setTimeout(() => {
                                viewImage(targetImageId);
                            }, 300);
                        } else {
                            ElMessage.warning('指定的图片不存在');
                        }
                    }


                    // 去掉URL参数
                    updateUrlWithoutParams();
                }
            }
        };


        const loadAlbumImages = async (albumId) => {
            try {
                const headers = {};

                // // 如果是加密相册且有访问token，添加到请求头
                // const token = albumAccessTokens.value[albumId];
                // if (token) {
                //     headers['X-Album-Auth'] = token;
                // }

                // 如果是管理员，添加管理员token到请求头
                if (isAdmin.value && adminToken.value) {
                    headers['X-Admin-Token'] = adminToken.value;
                } else {
                    // 普通用户才检查相册访问token
                    const token = albumAccessTokens.value[albumId];
                    if (token) {
                        headers['X-Album-Auth'] = token;
                    }
                }


                const response = await fetch(`/api/albums/${albumId}/images`, {
                    headers: headers
                });

                if (response.status === 403) {
                    // 无权限访问，清除token
                    delete albumAccessTokens.value[albumId];
                    localStorage.removeItem(`album_${albumId}_token`);

                    // 获取相册信息并弹出密码框
                    const album = albums.value.find(a => a.id === albumId);
                    if (album) {
                        // 等待密码验证结果
                        const success = await showPasswordDialog(album);
                        // 如果用户取消，返回false，不进入相册
                        if (!success) {
                            return false;
                        }
                        // 如果验证成功，重新调用自己（因为现在有token了）
                        return await loadAlbumImages(albumId);
                    }
                    return false;
                }

                if (!response.ok) {
                    throw new Error('加载失败');
                }

                images.value = await response.json();
                sortImages();

                return true;
            } catch (error) {
                ElMessage.error('加载图片失败');
                return false;
            }
        };


        const loadSmartAlbums = async () => {
            if (!isAdmin.value || !window.SmartAlbumApi) {
                smartAlbums.value = [];
                return;
            }
            try {
                const data = await window.SmartAlbumApi.list();
                smartAlbums.value = Array.isArray(data.albums) ? data.albums : [];
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                console.error('加载 Smart Album 失败:', error);
            }
        };

        const openSmartAlbum = async (album) => {
            if (!album) return;
            currentSmartAlbum.value = {...album};
            smartAlbumImages.value = [];
            smartAlbumQueryError.value = '';
            currentFilter.value = 'all';
            currentView.value = 'smart-album';
            await runSmartAlbum();
        };

        const formatExplorePercent = (value) => {
            const number = Number(value || 0);
            if (!Number.isFinite(number)) return '0.0%';
            return `${number.toFixed(1)}%`;
        };

        const exploreSourceText = computed(() => {
            const sources = Array.isArray(exploreStats.value?.sources) ? exploreStats.value.sources : [];
            return sources.length ? sources.map(source => source.name).join(' · ') : '—';
        });

        const exploreYearOptions = computed(() => (Array.isArray(exploreStats.value?.years) ? exploreStats.value.years : [])
            .filter(item => Number.isFinite(Number(item && item.value)))
            .slice()
            .sort((a, b) => Number(b.value) - Number(a.value)));
        const exploreSelectedYearRow = computed(() => exploreYearOptions.value.find(item =>
            Number(item.value) === Number(exploreSelectedYear.value)
        ) || null);
        const exploreMonthRows = computed(() => exploreSelectedYearRow.value?.months || []);
        const visibleExploreRows = (section, rows) => {
            const items = Array.isArray(rows) ? rows : [];
            return exploreExpandedSections.value[section] ? items : items.slice(0, EXPLORE_COLLAPSED_ITEMS);
        };
        const exploreCanExpand = rows => Array.isArray(rows) && rows.length > EXPLORE_COLLAPSED_ITEMS;
        const exploreHiddenCount = rows => Math.max(0, (Array.isArray(rows) ? rows.length : 0) - EXPLORE_COLLAPSED_ITEMS);
        const toggleExploreSection = section => {
            if (!Object.prototype.hasOwnProperty.call(exploreExpandedSections.value, section)) return;
            exploreExpandedSections.value = {
                ...exploreExpandedSections.value,
                [section]: !exploreExpandedSections.value[section],
            };
        };

        const loadExploreStats = async () => {
            if (!isAdmin.value || !window.ExploreApi) return;
            exploreLoading.value = true;
            exploreError.value = '';
            try {
                const data = await window.ExploreApi.stats();
                exploreStats.value = {...emptyExploreStats(), ...data};
                const years = (Array.isArray(data.years) ? data.years : [])
                    .map(item => Number(item && item.value))
                    .filter(Number.isFinite)
                    .sort((a, b) => b - a);
                if (!years.includes(Number(exploreSelectedYear.value))) {
                    exploreSelectedYear.value = years.length ? years[0] : null;
                }
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                exploreStats.value = emptyExploreStats();
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                exploreError.value = error.message || 'Explore 统计加载失败';
            } finally {
                exploreLoading.value = false;
            }
        };

        const openExplore = async () => {
            if (!isAdmin.value) return;
            currentView.value = 'explore';
            currentImage.value = {};
            selectionMode.value = false;
            selectedImages.value = [];
            await loadExploreStats();
            await nextTick();
            window.requestAnimationFrame(() => window.scrollTo(0, 0));
        };

        const openExploreStat = async (dimension, value, label, target = 'photos') => {
            if (!window.ExploreApi || exploreQueryLoading.value) return;
            exploreReturnScrollY = window.scrollY || window.pageYOffset || 0;
            exploreQueryLoading.value = true;
            try {
                const data = await window.ExploreApi.query(dimension, value, label, target);
                if (data.result_type === 'sets') {
                    smartSetEntryContext = null;
                    currentSmartSet.value = {
                        id: null,
                        name: data.title || `Explore · ${label || ''}`,
                        description: 'Explore 临时结果 · 未保存为 Smart Set',
                        is_explore: true,
                        explore_dimension: dimension,
                        explore_value: value,
                        explore_target: 'sets',
                    };
                    smartSetResults.value = (data.sets || []).map((item, index) => ({
                        ...item,
                        smart_query_order: index,
                    }));
                    smartSetDirectoryItems.value = [];
                    smartSetQueryError.value = '';
                    setSearchQuery.value = '';
                    await hydrateSmartSetDirectoryItems(smartSetResults.value);
                    if (data.index) smartAlbumIndex.value = data.index;
                    currentView.value = 'smart-set';
                    await nextTick();
                    window.requestAnimationFrame(() => window.scrollTo(0, 0));
                    return;
                }

                currentSmartAlbum.value = {
                    id: null,
                    name: data.title || `Explore · ${label || ''}`,
                    description: 'Explore 临时结果 · 未保存为 Smart Album',
                    is_explore: true,
                    explore_dimension: dimension,
                    explore_value: value,
                    explore_target: 'photos',
                };
                smartAlbumImages.value = (data.images || []).map((image, index) => ({
                    ...image,
                    smart_album_id: 'explore',
                    explore_result: true,
                    smart_query_order: index,
                }));
                smartAlbumQueryError.value = '';
                smartAlbumSort.value = {field: 'query_order', order: 'asc'};
                currentFilter.value = 'all';
                if (data.index) smartAlbumIndex.value = data.index;
                currentView.value = 'smart-album';
                await nextTick();
                window.requestAnimationFrame(() => window.scrollTo(0, 0));
            } catch (error) {
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                ElMessage.error(error.message || 'Explore 查询失败');
            } finally {
                exploreQueryLoading.value = false;
            }
        };

        const backToExplore = async () => {
            smartSetEntryContext = null;
            currentView.value = 'explore';
            currentSmartAlbum.value = {};
            smartAlbumImages.value = [];
            smartAlbumQueryError.value = '';
            currentSmartSet.value = {};
            smartSetResults.value = [];
            smartSetDirectoryItems.value = [];
            smartSetQueryError.value = '';
            setSearchQuery.value = '';
            currentImage.value = {};
            selectionMode.value = false;
            selectedImages.value = [];
            await nextTick();
            window.requestAnimationFrame(() => window.scrollTo(0, Math.max(0, exploreReturnScrollY || 0)));
        };

        const runSmartAlbum = async () => {
            if (!currentSmartAlbum.value?.id || !window.SmartAlbumApi) return;
            if (!smartAlbumIndex.value?.ready) {
                smartAlbumImages.value = [];
                smartAlbumQueryError.value = 'Smart Album 索引尚未建立。请先点击“刷新索引”，完成后再运行查询。';
                return;
            }
            smartAlbumLoading.value = true;
            smartAlbumQueryError.value = '';
            try {
                const data = await window.SmartAlbumApi.run(currentSmartAlbum.value.id);
                if (data.album) currentSmartAlbum.value = {...currentSmartAlbum.value, ...data.album};
                smartAlbumImages.value = (data.images || []).map((image, index) => ({
                    ...image,
                    smart_album_id: currentSmartAlbum.value.id,
                    smart_query_order: index,
                }));
                if (data.index) smartAlbumIndex.value = data.index;
                await loadSmartAlbums();
            } catch (error) {
                smartAlbumImages.value = [];
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                const trace = error?.payload?.traceback || '';
                smartAlbumQueryError.value = trace ? `${error.message}

${trace}` : (error.message || 'Smart Album 执行失败');
                if (error?.payload?.code === 'smart_album_index_required') {
                    ElMessage.warning(error.message || '请先刷新 Smart Album 索引');
                } else {
                    ElMessage.error(error.message || 'Smart Album 执行失败');
                }
            } finally {
                smartAlbumLoading.value = false;
            }
        };

        const smartAlbumStepStatusText = (step) => {
            const status = step?.status || 'pending';
            if (status === 'done') return '已完成';
            if (status === 'active') return '进行中';
            if (status === 'error') return '失败';
            if (status === 'cancelled') return '已取消';
            return '等待中';
        };

        const smartAlbumStepTagType = (step) => {
            const status = step?.status || 'pending';
            if (status === 'done') return 'success';
            if (status === 'error') return 'danger';
            if (status === 'active') return 'warning';
            return 'info';
        };

        const smartAlbumStepPercent = (step) => {
            const total = Number(step?.total || 0);
            const current = Number(step?.current || 0);
            if (total <= 0) return step?.status === 'done' ? 100 : 0;
            return Math.max(0, Math.min(100, Math.round(current * 100 / total)));
        };

        const smartAlbumStepSummary = (steps) => {
            const list = Array.isArray(steps) ? steps : [];
            const done = list.filter(step => step?.status === 'done').length;
            const remaining = list.filter(step => !['done', 'error'].includes(step?.status)).length;
            return `已完成 ${done} / ${list.length} 步 · 剩余 ${remaining} 步`;
        };

        const formatSmartAlbumStageCounts = (summary) => {
            const counts = summary?.stage_counts || {};
            const labels = [
                ['base_edit', 'Base'],
                ['model_edit', 'Model'],
                ['revision', 'Revision'],
                ['final', 'Final'],
                ['original_jpg', 'Original JPG']
            ];
            return labels
                .filter(([key]) => Object.prototype.hasOwnProperty.call(counts, key))
                .map(([key, label]) => `${label} ${counts[key] || 0}`)
                .join(' · ');
        };

        const waitForSmartAlbumIndex = async () => {
            while (true) {
                const progress = await window.SmartAlbumApi.indexProgress();
                smartAlbumIndexProgress.value = progress;
                if (!progress.active) {
                    if (progress.error) throw new Error(progress.error);
                    return progress;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        };

        const refreshSmartAlbumIndex = async () => {
            if (!window.SmartAlbumApi) return;
            showSmartAlbumIndexDialog.value = true;
            try {
                await loadLibrarySources();
                const progress = await window.SmartAlbumApi.indexProgress();
                if (progress.index) smartAlbumIndex.value = progress.index;
                if (!progress.active) {
                    smartAlbumIndexProgress.value = emptySmartAlbumIndexProgress();
                    initializeSmartAlbumIndexSourceSelection();
                    return;
                }
                smartAlbumIndexProgress.value = progress;
                initializeSmartAlbumIndexSourceSelection(progress);
                if (!smartAlbumIndexRefreshing.value) {
                    smartAlbumIndexRefreshing.value = true;
                    void (async () => {
                        try {
                            const finalProgress = await waitForSmartAlbumIndex();
                            smartAlbumIndex.value = finalProgress.index || await window.SmartAlbumApi.indexStatus();
                        } catch (error) {
                            ElMessage.error(error.message || '读取 Smart Album 索引进度失败');
                        } finally {
                            smartAlbumIndexRefreshing.value = false;
                        }
                    })();
                }
            } catch (error) {
                ElMessage.error(error.message || '读取 Smart Album 索引状态失败');
            }
        };

        const startSmartAlbumIndexRefresh = async () => {
            if (!window.SmartAlbumApi || smartAlbumIndexRefreshing.value) return;
            const selectedSourceIds = smartAlbumIndexSelectedSourceIds.value.map(Number);
            if (!selectedSourceIds.length) {
                ElMessage.warning('请至少选择一个需要建立索引的 Source');
                return;
            }
            smartAlbumIndexRefreshing.value = true;
            smartAlbumIndexProgress.value = {
                ...emptySmartAlbumIndexProgress(),
                active: true,
                phase: 'starting',
                message: '准备刷新 Smart Album 索引',
                summary: {
                    ...emptySmartAlbumIndexProgress().summary,
                    selected_source_ids: selectedSourceIds,
                    selected_source_names: enabledSmartAlbumIndexSources.value
                        .filter(source => selectedSourceIds.includes(Number(source.id)))
                        .map(source => source.name)
                }
            };
            try {
                const started = await window.SmartAlbumApi.refreshIndex(selectedSourceIds);
                smartAlbumIndexProgress.value = started;
                const finalProgress = await waitForSmartAlbumIndex();
                const status = finalProgress.index || await window.SmartAlbumApi.indexStatus();
                smartAlbumIndex.value = status;
                if (finalProgress.phase === 'cancelled') {
                    ElMessage.info('Smart Album 索引刷新已取消；上一次可用索引已保留');
                    return;
                }
                ElMessage.success(`Smart Album 索引已刷新：${status.asset_count || 0} 张图片`);
                if (currentView.value === 'smart-album' && currentSmartAlbum.value?.id) {
                    await runSmartAlbum();
                }
            } catch (error) {
                smartAlbumIndexProgress.value = {...smartAlbumIndexProgress.value, active: false, phase: 'error', error: error.message || '刷新失败'};
                ElMessage.error(error.message || '刷新 Smart Album 索引失败');
            } finally {
                smartAlbumIndexRefreshing.value = false;
            }
        };

        const cancelSmartAlbumIndexRefresh = async () => {
            if (!window.SmartAlbumApi || !smartAlbumIndexRefreshing.value) return;
            try {
                const progress = await window.SmartAlbumApi.cancelIndex();
                smartAlbumIndexProgress.value = progress;
            } catch (error) {
                ElMessage.error(error.message || '取消 Smart Album 索引刷新失败');
            }
        };

        const initializeSmartAlbumSyncSourceSelection = () => {
            const availableIds = enabledSmartAlbumIndexSources.value
                .filter(source => !!source.available)
                .map(source => Number(source.id));
            const allowed = new Set(availableIds);
            const indexedIds = Array.isArray(smartAlbumIndex.value?.indexed_source_ids)
                ? smartAlbumIndex.value.indexed_source_ids.map(Number)
                : [];
            const selected = indexedIds.filter(sourceId => allowed.has(sourceId));
            smartAlbumSyncSelectedSourceIds.value = selected.length ? selected : [...availableIds];
        };

        const invalidateSmartAlbumSyncPlan = () => {
            if (smartAlbumSyncActive.value) return;
            smartAlbumSyncPlan.value = null;
            smartAlbumSyncProgress.value = emptySmartAlbumSyncProgress();
        };

        const waitForSmartAlbumSync = async () => {
            while (true) {
                const progress = await window.SmartAlbumApi.indexSyncProgress();
                smartAlbumSyncProgress.value = progress;
                if (progress.plan) smartAlbumSyncPlan.value = progress.plan;
                if (!progress.active) {
                    if (progress.error) throw new Error(progress.error);
                    return progress;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        };

        const openSmartAlbumIndexSync = async () => {
            if (!window.SmartAlbumApi) return;
            showSmartAlbumSyncDialog.value = true;
            try {
                await loadLibrarySources();
                const progress = await window.SmartAlbumApi.indexSyncProgress();
                if (progress.index) smartAlbumIndex.value = progress.index;
                if (progress.active) {
                    smartAlbumSyncProgress.value = progress;
                    smartAlbumSyncPlan.value = progress.plan || null;
                    smartAlbumSyncSelectedSourceIds.value = Array.isArray(progress.plan?.source_ids)
                        ? progress.plan.source_ids.map(Number)
                        : [];
                    if (!smartAlbumSyncActive.value) {
                        smartAlbumSyncActive.value = true;
                        void (async () => {
                            try {
                                const finalProgress = await waitForSmartAlbumSync();
                                smartAlbumIndex.value = finalProgress.index || await window.SmartAlbumApi.indexStatus();
                            } catch (error) {
                                ElMessage.error(error.message || '读取 Smart Album 同步进度失败');
                            } finally {
                                smartAlbumSyncActive.value = false;
                            }
                        })();
                    }
                    return;
                }
                smartAlbumSyncProgress.value = emptySmartAlbumSyncProgress();
                smartAlbumSyncPlan.value = null;
                initializeSmartAlbumSyncSourceSelection();
            } catch (error) {
                ElMessage.error(error.message || '读取 Smart Album 同步状态失败');
            }
        };

        const scanSmartAlbumIndexChanges = async () => {
            if (!window.SmartAlbumApi || smartAlbumSyncActive.value || smartAlbumSyncScanning.value) return;
            const selectedSourceIds = smartAlbumSyncSelectedSourceIds.value.map(Number);
            if (!selectedSourceIds.length) {
                ElMessage.warning('请至少选择一个需要同步的 Source');
                return;
            }
            const unavailable = enabledSmartAlbumIndexSources.value.filter(
                source => selectedSourceIds.includes(Number(source.id)) && !source.available
            );
            if (unavailable.length) {
                ElMessage.warning(`以下 Source 当前不可用：${unavailable.map(source => source.name).join('、')}`);
                return;
            }
            smartAlbumSyncScanning.value = true;
            try {
                const plan = await window.SmartAlbumApi.previewIndexSync(selectedSourceIds);
                smartAlbumSyncPlan.value = plan;
                smartAlbumSyncProgress.value = emptySmartAlbumSyncProgress();
                const summary = plan.summary || {};
                const changes = Number(summary.added_count || 0) + Number(summary.changed_count || 0) + Number(summary.deleted_count || 0);
                if (changes) {
                    ElMessage.success(`扫描完成：新增 ${summary.added_count || 0} · 修改 ${summary.changed_count || 0} · 删除 ${summary.deleted_count || 0}`);
                } else {
                    ElMessage.success('扫描完成：所选 Source 索引已是最新');
                }
            } catch (error) {
                smartAlbumSyncPlan.value = null;
                ElMessage.error(error.message || '扫描 Smart Album 索引变化失败');
            } finally {
                smartAlbumSyncScanning.value = false;
            }
        };

        const startSmartAlbumIndexSync = async () => {
            if (!window.SmartAlbumApi || smartAlbumSyncActive.value || !smartAlbumSyncPlan.value?.plan_id) return;
            const summary = smartAlbumSyncPlan.value.summary || {};
            const changeTotal = Number(summary.added_count || 0) + Number(summary.changed_count || 0) + Number(summary.deleted_count || 0);
            if (!changeTotal) {
                ElMessage.info('当前扫描计划没有需要同步的变化');
                return;
            }
            smartAlbumSyncActive.value = true;
            try {
                const started = await window.SmartAlbumApi.startIndexSync(smartAlbumSyncPlan.value.plan_id);
                smartAlbumSyncProgress.value = started;
                if (started.plan) smartAlbumSyncPlan.value = started.plan;
                const finalProgress = await waitForSmartAlbumSync();
                const status = finalProgress.index || await window.SmartAlbumApi.indexStatus();
                smartAlbumIndex.value = status;
                if (finalProgress.phase === 'cancelled') {
                    ElMessage.info('Smart Album 索引同步已取消；同步前索引保持不变');
                    return;
                }
                ElMessage.success(finalProgress.message || 'Smart Album 索引同步完成');
                if (currentView.value === 'smart-album' && currentSmartAlbum.value?.id) {
                    await runSmartAlbum();
                }
            } catch (error) {
                smartAlbumSyncProgress.value = {
                    ...smartAlbumSyncProgress.value,
                    active: false,
                    phase: 'error',
                    error: error.message || '同步失败'
                };
                ElMessage.error(error.message || '同步 Smart Album 索引失败');
            } finally {
                smartAlbumSyncActive.value = false;
            }
        };

        const cancelSmartAlbumIndexSync = async () => {
            if (!window.SmartAlbumApi || !smartAlbumSyncActive.value) return;
            try {
                smartAlbumSyncProgress.value = await window.SmartAlbumApi.cancelIndexSync();
            } catch (error) {
                ElMessage.error(error.message || '取消 Smart Album 索引同步失败');
            }
        };

        const openSmartAlbumHelp = async () => {
            showSmartAlbumHelpDialog.value = true;
            if (smartAlbumRuntime.value || !window.SmartAlbumApi) return;
            smartAlbumHelpLoading.value = true;
            try {
                smartAlbumRuntime.value = await window.SmartAlbumApi.runtime();
            } catch (error) {
                ElMessage.error(error.message || '读取 Smart Album Python 帮助失败');
            } finally {
                smartAlbumHelpLoading.value = false;
            }
        };

        const editSmartAlbum = (album = currentSmartAlbum.value) => {
            if (!album?.id) return;
            smartAlbumEditor.value = {
                id: album.id,
                name: album.name || '',
                description: album.description || '',
                python_code: album.python_code || SMART_ALBUM_DEFAULT_CODE
            };
            showEditSmartAlbumDialog.value = true;
        };

        const saveSmartAlbum = async () => {
            if (!smartAlbumEditor.value.id || !smartAlbumEditor.value.name.trim()) {
                ElMessage.warning('请输入相册名称');
                return;
            }
            try {
                const data = await window.SmartAlbumApi.update(smartAlbumEditor.value.id, {
                    name: smartAlbumEditor.value.name,
                    description: smartAlbumEditor.value.description,
                    python_code: smartAlbumEditor.value.python_code
                });
                showEditSmartAlbumDialog.value = false;
                if (data.album) currentSmartAlbum.value = {...currentSmartAlbum.value, ...data.album};
                await loadSmartAlbums();
                if (currentView.value === 'smart-album') await runSmartAlbum();
                ElMessage.success('Smart Album 已保存');
            } catch (error) {
                ElMessage.error(error.message || '保存 Smart Album 失败');
            }
        };

        const deleteSmartAlbum = async (album = currentSmartAlbum.value) => {
            if (!album?.id) return;
            try {
                await ElMessageBox.confirm(
                    `确定删除 Smart Album “${album.name || ''}”吗？只会删除查询定义，不会删除任何照片。`,
                    '删除 Smart Album',
                    {confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning'}
                );
                await window.SmartAlbumApi.remove(album.id);
                ElMessage.success('Smart Album 已删除');
                if (currentView.value === 'smart-album') backToAlbums();
                else await loadSmartAlbums();
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') {
                    ElMessage.error(error.message || '删除 Smart Album 失败');
                }
            }
        };

        const loadSmartSets = async () => {
            if (!isAdmin.value || !window.SmartSetApi) {
                smartSets.value = [];
                return;
            }
            try {
                const data = await window.SmartSetApi.list();
                smartSets.value = Array.isArray(data.sets) ? data.sets : [];
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                console.error('加载 Smart Set 失败:', error);
            }
        };

        const loadSmartViews = async () => {
            await Promise.all([loadSmartAlbums(), loadSmartSets()]);
        };

        const openSmartView = async (item) => {
            if (!item) return;
            if (item.smart_view_type === 'set') {
                await openSmartSet(item);
                return;
            }
            await openSmartAlbum(item);
        };

        const smartViewResultText = (item) => {
            if (!item || item.last_result_count == null) return '结果: —';
            return item.smart_view_type === 'set'
                ? `结果: ${item.last_result_count} Set`
                : `结果: ${item.last_result_count} 张`;
        };

        const openCreateSmartView = () => {
            smartViewCreatePreviousType = 'album';
            smartViewCreateEditor.value = {
                type: 'album',
                name: '',
                description: '',
                python_code: SMART_ALBUM_DEFAULT_CODE,
            };
            showCreateSmartViewDialog.value = true;
        };

        const changeSmartViewCreateType = (nextType) => {
            const previousDefault = smartViewCreatePreviousType === 'set'
                ? SMART_SET_DEFAULT_CODE
                : SMART_ALBUM_DEFAULT_CODE;
            const nextDefault = nextType === 'set' ? SMART_SET_DEFAULT_CODE : SMART_ALBUM_DEFAULT_CODE;
            const currentCode = String(smartViewCreateEditor.value.python_code || '');
            if (!currentCode.trim() || currentCode === previousDefault) {
                smartViewCreateEditor.value.python_code = nextDefault;
            }
            smartViewCreatePreviousType = nextType;
        };

        const openSmartViewCreateHelp = async () => {
            if (smartViewCreateEditor.value.type === 'set') {
                await openSmartSetHelp();
                return;
            }
            await openSmartAlbumHelp();
        };

        const createSmartView = async () => {
            const editor = smartViewCreateEditor.value;
            const name = String(editor.name || '').trim();
            if (!name) {
                ElMessage.warning('请输入智能视图名称');
                return;
            }
            const isSet = editor.type === 'set';
            const api = isSet ? window.SmartSetApi : window.SmartAlbumApi;
            if (!api) {
                ElMessage.error('智能视图 API 不可用');
                return;
            }
            try {
                await api.create({
                    name,
                    description: editor.description || '',
                    python_code: editor.python_code || (isSet ? SMART_SET_DEFAULT_CODE : SMART_ALBUM_DEFAULT_CODE),
                });
                showCreateSmartViewDialog.value = false;
                await loadSmartViews();
                ElMessage.success(`${isSet ? 'Smart Set' : 'Smart Album'} 创建成功`);
            } catch (error) {
                ElMessage.error(error.message || '创建智能视图失败');
            }
        };

        const handleSmartViewCommand = async (command) => {
            if (command === 'refresh-index') {
                await refreshSmartAlbumIndex();
                return;
            }
            if (command === 'sync-index') {
                await openSmartAlbumIndexSync();
            }
        };

        const hydrateSmartSetDirectoryItems = async (rows) => {
            const results = Array.isArray(rows) ? rows : [];
            if (!results.length) {
                smartSetDirectoryItems.value = [];
                return;
            }

            // Reuse the exact Library Set-parent payload, including the global folder-cover
            // setting, manifest status, and recursive directory/image/file counts.
            const groups = new Map();
            for (const item of results) {
                const sourceId = Number(item && item.source_id);
                const setPath = String(item && item.set_path || '');
                if (!Number.isFinite(sourceId) || !setPath) continue;
                const parts = setPath.split('/').filter(Boolean);
                const parentPath = parts.slice(0, -1).join('/');
                const key = `${sourceId}:${parentPath}`;
                if (!groups.has(key)) groups.set(key, {sourceId, parentPath, rows: []});
                groups.get(key).rows.push(item);
            }

            const hydratedByKey = new Map();
            await Promise.all(Array.from(groups.values()).map(async group => {
                const response = await fetch(`/api/library/sources/${group.sourceId}/browse?path=${encodeURIComponent(group.parentPath)}`);
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '读取 Set parent 失败');
                const directories = (data.items || []).filter(item => item.type === 'directory');
                const byPath = new Map(directories.map(item => [String(item.relative_path || ''), item]));
                for (const result of group.rows) {
                    const setPath = String(result.set_path || '');
                    const directory = byPath.get(setPath);
                    if (!directory) continue;
                    hydratedByKey.set(`${group.sourceId}:${setPath}`, {
                        ...directory,
                        source_id: group.sourceId,
                        source_name: result.source_name || '',
                        set_path: setPath,
                        smart_set_result_id: result.id,
                    });
                }
            }));

            smartSetDirectoryItems.value = results
                .map(item => hydratedByKey.get(`${Number(item.source_id)}:${String(item.set_path || '')}`))
                .filter(Boolean);
        };

        const openSmartSet = async (item) => {
            if (!item) return;
            smartSetEntryContext = null;
            currentSmartSet.value = {...item};
            smartSetResults.value = [];
            smartSetDirectoryItems.value = [];
            smartSetQueryError.value = '';
            setSearchQuery.value = '';
            currentView.value = 'smart-set';
            await runSmartSet();
        };

        const runSmartSet = async () => {
            if (!currentSmartSet.value?.id || !window.SmartSetApi) return;
            smartSetLoading.value = true;
            smartSetQueryError.value = '';
            try {
                const data = await window.SmartSetApi.run(currentSmartSet.value.id);
                if (data.smart_set) currentSmartSet.value = {...currentSmartSet.value, ...data.smart_set};
                smartSetResults.value = (data.sets || []).map((item, index) => ({...item, smart_query_order: index}));
                await hydrateSmartSetDirectoryItems(smartSetResults.value);
                if (data.index) smartAlbumIndex.value = data.index;
                await loadSmartSets();
            } catch (error) {
                smartSetResults.value = [];
                smartSetDirectoryItems.value = [];
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                const trace = error?.payload?.traceback || '';
                smartSetQueryError.value = trace ? `${error.message}\n\n${trace}` : (error.message || 'Smart Set 执行失败');
                ElMessage.error(error.message || 'Smart Set 执行失败');
            } finally {
                smartSetLoading.value = false;
            }
        };

        const editSmartSet = (item = currentSmartSet.value) => {
            if (!item?.id) return;
            smartSetEditor.value = {
                id: item.id,
                name: item.name || '',
                description: item.description || '',
                python_code: item.python_code || SMART_SET_DEFAULT_CODE
            };
            showEditSmartSetDialog.value = true;
        };

        const saveSmartSet = async () => {
            if (!smartSetEditor.value.id || !smartSetEditor.value.name.trim()) {
                ElMessage.warning('请输入 Smart Set 名称');
                return;
            }
            try {
                const data = await window.SmartSetApi.update(smartSetEditor.value.id, {
                    name: smartSetEditor.value.name,
                    description: smartSetEditor.value.description,
                    python_code: smartSetEditor.value.python_code
                });
                showEditSmartSetDialog.value = false;
                if (data.smart_set) currentSmartSet.value = {...currentSmartSet.value, ...data.smart_set};
                await loadSmartSets();
                if (currentView.value === 'smart-set') await runSmartSet();
                ElMessage.success('Smart Set 已保存');
            } catch (error) {
                ElMessage.error(error.message || '保存 Smart Set 失败');
            }
        };

        const deleteSmartSet = async (item = currentSmartSet.value) => {
            if (!item?.id) return;
            try {
                await ElMessageBox.confirm(
                    `确定删除 Smart Set “${item.name || ''}”吗？只会删除查询定义，不会修改任何 Set 或照片。`,
                    '删除 Smart Set',
                    {confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning'}
                );
                await window.SmartSetApi.remove(item.id);
                if (currentView.value === 'smart-set') backToAlbums();
                await loadSmartSets();
                ElMessage.success('Smart Set 已删除');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || '删除 Smart Set 失败');
            }
        };

        const openSmartSetHelp = async () => {
            showSmartSetHelpDialog.value = true;
            if (smartSetRuntime.value || !window.SmartSetApi) return;
            smartSetHelpLoading.value = true;
            try {
                smartSetRuntime.value = await window.SmartSetApi.runtime();
            } catch (error) {
                ElMessage.error(error.message || '读取 Smart Set Python 帮助失败');
            } finally {
                smartSetHelpLoading.value = false;
            }
        };

        const smartSetContextKey = item => {
            if (!item) return '';
            if (item.is_explore) {
                return `explore:${item.explore_dimension || ''}:${JSON.stringify(item.explore_value ?? null)}:${item.explore_target || 'sets'}`;
            }
            return item.id == null ? '' : `saved:${item.id}`;
        };

        const openSmartSetResult = async (item, event = null) => {
            if (!item) return;
            const source = librarySources.value.find(source => Number(source.id) === Number(item.source_id));
            if (!source) {
                ElMessage.error('对应的 Library Source 不可用');
                return;
            }

            const setPath = String(item.set_path || item.relative_path || '');
            const target = event && event.currentTarget instanceof Element ? event.currentTarget : null;
            const pendingContext = {
                smartSetKey: smartSetContextKey(currentSmartSet.value),
                sourceId: Number(source.id),
                setPath,
                scrollY: window.scrollY || window.pageYOffset || 0,
                viewportTop: target ? target.getBoundingClientRect().top : null,
            };

            await openLibrarySource(source, setPath);

            if (
                currentView.value === 'library'
                && Number(currentLibrarySource.value && currentLibrarySource.value.id) === pendingContext.sourceId
                && String(libraryListing.value && libraryListing.value.path || '') === pendingContext.setPath
            ) {
                smartSetEntryContext = pendingContext;
            }
        };

        const createAlbum = async () => {
            if (!newAlbum.value.name) {
                ElMessage.warning('请输入相册名称');
                return;
            }

            try {
                const response = await fetch('/api/albums', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        // 'X-Admin-Token': adminToken.value
                    },
                    body: JSON.stringify(newAlbum.value)
                });

                if (response.ok) {
                    ElMessage.success('相册创建成功');
                    showCreateAlbumDialog.value = false;
                    resetNewAlbumForm(false);
                    loadAlbums();
                } else {
                    // 获取后端返回的错误信息
                    const data = await response.json();
                    ElMessage.error('创建相册失败: ' + (data.error || '未知错误'));
                }
            } catch (error) {
                ElMessage.error('创建相册失败');
            }
        };


        const updateAlbum = async () => {
            try {
                const updateData = {
                    name: currentAlbum.value.name,
                    description: currentAlbum.value.description,
                    shoot_date: currentAlbum.value.shoot_date,
                    model_name: currentAlbum.value.model_name,
                    location: currentAlbum.value.location,
                    cover_image_id: currentAlbum.value.cover_image_id,
                    group_ids: currentAlbum.value.group_ids || []
                };

                const response = await fetch(`/api/albums/${currentAlbum.value.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(updateData)
                });

                if (response.ok) {
                    // 密码验证
                    if (passwordEnabled.value && newPassword.value.trim()) {
                        await setAlbumPassword();
                    }

                    ElMessage.success('相册更新成功');
                    showEditAlbumDialog.value = false;

                    // 重新加载相册列表
                    loadAlbums();

                    // 重新加载当前相册的分组信息
                    await loadAlbumGroupsInfo(currentAlbum.value.id);
                } else {
                    const errorData = await response.json();
                    ElMessage.error(errorData.error || '更新相册失败');
                }
            } catch (error) {
                console.error('更新相册失败:', error);
                ElMessage.error('更新相册失败');
            }
        };

        const deleteAlbum = async (albumId) => {
            try {
                await ElMessageBox.confirm('确定要删除这个相册吗？相册中的所有图片也将被删除。', '警告', {
                    confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning'
                });
                const response = await fetch(`/api/albums/${albumId}`, {method: 'DELETE'});
                if (response.ok) {
                    ElMessage.success('相册删除成功');
                    backToAlbums();
                    loadAlbums();
                } else {
                    const data = await response.json();
                    ElMessage.error('删除相册失败: ' + (data.error || '未知错误'));
                }
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('删除相册失败');
            }
        };


        const backToAlbums = () => {
            smartSetEntryContext = null;
            currentView.value = 'albums';
            currentAlbum.value = {};
            currentSmartAlbum.value = {};
            smartAlbumImages.value = [];
            smartAlbumQueryError.value = '';
            currentSmartSet.value = {};
            smartSetResults.value = [];
            smartSetDirectoryItems.value = [];
            smartSetQueryError.value = '';
            images.value = [];
            selectionMode.value = false;
            selectedImages.value = [];

            // 重置筛选和排序状态
            currentFilter.value = 'all';

            loadAlbums();
            if (isAdmin.value) {
                loadSmartViews();
            }
        };

        const backToAlbum = () => {
            if (currentImage.value && currentImage.value.smart_album_id) {
                currentView.value = 'smart-album';
                currentImage.value = {};
                return;
            }
            if (currentImage.value && currentImage.value.source_type === 'library-share') {
                currentView.value = 'library-share';
                currentImage.value = {};
                return;
            }
            if (currentImage.value && currentImage.value.source_type === 'library') {
                currentView.value = 'library';
                currentImage.value = {};
                return;
            }
            currentView.value = 'album-detail';
            currentImage.value = {};

            selectionMode.value = false;
            selectedImages.value = [];
        };

        const viewImage = async (imageId) => {
            const image = filteredImages.value.find(img => img.id === imageId);
            if (!image) return;
            if (image.source_type === 'library') {
                await viewLibraryImage(image);
                return;
            }
            if (image.source_type === 'library-share') {
                await viewSharedImage(image);
                return;
            }
            currentImage.value = image;
            currentView.value = 'image-detail';
        };


        const handleUploadSuccess = (response, file, fileList) => {
            if (response.immediate_response) {
                // 如果是队列处理，显示队列信息
                ElMessage.info(`图片已加入处理队列`);

                // 可以添加一个定时器来检查处理状态
                const checkStatus = async (filename) => {
                    try {
                        const statusResponse = await fetch(`/api/upload/status/${filename}`);
                        const statusData = await statusResponse.json();

                        if (statusData.status === 'completed') {
                            ElMessage.success(`图片处理完成: ${file.name}`);

                            // 重新加载当前相册的图片
                            await loadAlbumImages(currentAlbum.value.id);
                            // 更新相册列表（更新图片数量）
                            await loadAlbums();

                        } else if (statusData.status === 'queued') {
                            // 继续轮询
                            setTimeout(() => checkStatus(filename), 2000);
                        } else if (statusData.status === 'processing') {
                            // 处理中，继续轮询
                            setTimeout(() => checkStatus(filename), 2000);
                        }
                    } catch (error) {
                        console.error('检查上传状态失败:', error);
                    }
                };

                // 开始检查状态
                setTimeout(() => checkStatus(response.filename), 2000);
            } else {
                // 原来的直接处理成功逻辑
                ElMessage.success('图片上传成功');
                loadAlbumImages(currentAlbum.value.id);
                // 更新相册列表（更新图片数量）
                loadAlbums();
            }
        };

        const handleUploadError = (error) => {
            try {
                // 尝试解析错误响应
                const errorData = JSON.parse(error.message || '{}');
                ElMessage.error(errorData.error || '图片上传失败');
            } catch (e) {
                ElMessage.error('图片上传失败');
            }
        };

        const beforeUpload = (file) => {
            const isLt100M = file.size / 1024 / 1024 < 100;
            if (!isLt100M) ElMessage.error('图片大小不能超过100MB!');
            return isLt100M;
        };

        const deleteImage = async (imageId, fromDetail = false) => {
            try {
                await ElMessageBox.confirm('确定要删除这张图片吗？', '警告', {
                    confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning'
                });

                // 保存当前图片索引，用于详情页删除后的导航
                const currentFilteredIndex = filteredImages.value.findIndex(img => img.id === imageId);

                const response = await fetch(`/api/images/${imageId}`, {method: 'DELETE'});
                if (response.ok) {
                    ElMessage.success('图片删除成功');

                    // 重新加载图片列表
                    await loadAlbumImages(currentAlbum.value.id);

                    // 重新加载相册列表（更新图片数量）
                    await loadAlbums();

                    if (fromDetail) {
                        // 在详情页删除的处理
                        // if (images.value.length === 0) {
                        if (filteredImages.value.length === 0) {
                            // 如果没有图片了，返回相册详情页
                            backToAlbum();
                        } else {

                            // 智能导航到合适的图片
                            let targetImage = null;

                            // 优先尝试显示下一张
                            if (currentFilteredIndex < filteredImages.value.length) {
                                targetImage = filteredImages.value[currentFilteredIndex];
                            }
                            // 如果没有下一张，显示上一张
                            else if (currentFilteredIndex > 0) {
                                targetImage = filteredImages.value[currentFilteredIndex - 1];
                            }
                            // 如果都不行，显示第一张
                            else if (filteredImages.value.length > 0) {
                                targetImage = filteredImages.value[0];
                            }

                            if (targetImage) {
                                currentImage.value = targetImage;
                            } else {
                                backToAlbum();
                            }
                        }
                    } else {
                        // 在列表页删除，保持原有逻辑
                    }

                    if (currentAlbum.value.cover_image_id === imageId) {
                        loadAlbums();
                    }
                } else {
                    const data = await response.json();
                    ElMessage.error('删除图片失败: ' + (data.error || '未知错误'));
                }
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('删除图片失败');
            }
        };

        const setAsCover = async (imageId) => {
            try {
                const response = await fetch(`/api/albums/${currentAlbum.value.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({cover_image_id: imageId})
                });
                if (response.ok) {
                    ElMessage.success('封面设置成功');
                    currentAlbum.value.cover_image_id = imageId;
                    loadAlbums();
                }
            } catch (error) {
                ElMessage.error('设置封面失败');
            }
        };

        const downloadImage = async (imageId) => {
            try {
                const urlToFetch = currentImage.value && (currentImage.value.source_type === 'library' || currentImage.value.source_type === 'library-share')
                    ? detailImageUrl(currentImage.value, 'original')
                    : `/api/images/${imageId}/file?type=original`;
                const response = await fetch(urlToFetch);
                if (!response.ok) throw new Error('download failed');
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = currentImage.value.original_filename;
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
            } catch (error) {
                ElMessage.error('下载图片失败');
            }
        };

        const formatDate = (dateString) => {
            if (!dateString) return '';
            return new Date(dateString).toLocaleDateString('zh-CN');
        };

        const formatFileSize = (bytes) => {
            if (!bytes) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        };


        // ==================== 本地目录映射 / Library ====================
        const librarySources = ref([]);
        const enabledSmartAlbumIndexSources = computed(() =>
            librarySources.value.filter(source => !!source.enabled)
        );

        const initializeSmartAlbumIndexSourceSelection = (progress = null) => {
            const enabledIds = enabledSmartAlbumIndexSources.value.map(source => Number(source.id));
            const allowed = new Set(enabledIds);
            const progressIds = Array.isArray(progress?.summary?.selected_source_ids)
                ? progress.summary.selected_source_ids.map(Number)
                : [];
            const indexedIds = Array.isArray(smartAlbumIndex.value?.indexed_source_ids)
                ? smartAlbumIndex.value.indexed_source_ids.map(Number)
                : [];
            const preferredIds = progressIds.length ? progressIds : (indexedIds.length ? indexedIds : enabledIds);
            const selected = preferredIds.filter(sourceId => allowed.has(sourceId));
            smartAlbumIndexSelectedSourceIds.value = selected.length ? selected : [...enabledIds];
        };

        const smartAlbumIndexedSourceText = computed(() => {
            const ids = new Set((smartAlbumIndex.value?.indexed_source_ids || []).map(Number));
            const liveNames = librarySources.value
                .filter(source => ids.has(Number(source.id)))
                .map(source => source.name);
            if (liveNames.length) return liveNames.join('、');
            const storedNames = Array.isArray(smartAlbumIndex.value?.indexed_source_names)
                ? smartAlbumIndex.value.indexed_source_names.filter(Boolean)
                : [];
            return storedNames.join('、');
        });

        const currentLibrarySource = ref(null);
        const libraryListing = ref({items: [], manifest: {exists: false}});
        const libraryLoading = ref(false);
        const showLibrarySourcesDialog = ref(false);
        const newLibrarySource = ref({name: 'Completed', root_path: ''});
        const showNewSetDialog = ref(false);
        const newSetCreating = ref(false);
        const newSetForm = ref({model: '', date: '', theme: ''});

        const showManifestDialog = ref(false);
        const showManifestDetailDialog = ref(false);
        const manifestEditPath = ref('');

        // Library navigation remembers the folder that was opened from each
        // directory, plus its viewport position. Returning to the parent then
        // restores both the selection and the exact place the user left.
        const libraryDirectoryNavigationState = new Map();
        const librarySelectedDirectoryPath = ref('');
        const manifestForm = ref({});
        const manifestKnownValues = ref({});
        const manifestEditorMode = ref('form');
        const manifestJsonText = ref('');
        const manifestJsonError = ref('');

        const gpsPhotoImport = window.GpsPhotoImporter.createController({
            getLocation: () => manifestForm.value && manifestForm.value.location,
            message: ElMessage
        });

        const manifestAutofill = window.ManifestAutofill.createController({
            getSourceId: () => currentLibrarySource.value && currentLibrarySource.value.id,
            getManifestForm: () => manifestForm.value,
            message: ElMessage
        });

        const workflowTools = window.WorkflowTools.createController({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path,
            refreshCurrent: async () => {
                if (currentLibrarySource.value && libraryListing.value && libraryListing.value.path !== null && libraryListing.value.path !== undefined) {
                    await loadLibraryDirectory(libraryListing.value.path || '');
                }
            }
        });

        const finalBuilderRulesVisible = ref(false);
        const finalMetadataRulesVisible = ref(false);

        const finalBuilder = window.FinalBuilder.createController({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path,
            refreshCurrent: async () => {
                if (currentLibrarySource.value && libraryListing.value && libraryListing.value.path !== null && libraryListing.value.path !== undefined) {
                    await loadLibraryDirectory(libraryListing.value.path || '');
                }
            },
            openMetadata: async () => {
                await finalMetadata.open();
            }
        });

        const finalMetadata = window.FinalMetadata.createController({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path,
            refreshCurrent: async () => {
                if (currentLibrarySource.value && libraryListing.value && libraryListing.value.path !== null && libraryListing.value.path !== undefined) {
                    await loadLibraryDirectory(libraryListing.value.path || '');
                }
            }
        });

        const setInsights = window.SetInsights.createController({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path
        });

        // Baseline select candidates. Values found in existing manifests are
        // merged at runtime so newly saved values become reusable automatically.
        const manifestOptions = {
            environment: ['studio', 'outdoor', 'indoor'],
            weather: ['sunny', 'cloudy', 'overcast', 'rainy', 'snowy'],
            genre: ['cosplay', 'jk', 'lolita', 'casual', 'jirai', 'kimono'],
            scene: [
                'white_studio/plain', 'white_studio/diorama', 'white_studio/scenic',
                'themed_studio/european', 'themed_studio/gothic', 'themed_studio/japanese',
                'themed_studio/chinese', 'themed_studio/bar', 'themed_studio/office',
                'themed_studio/hospital', 'themed_studio/church', 'themed_studio/cyber',
                'themed_studio/christmas',
                'school/classroom', 'school/equipment_room', 'school/campus',
                'home/living_room', 'home/bedroom', 'home/kitchen',
                'sports/gym', 'sports/pool', 'sports/tennis',
                'urban/street', 'urban/cafe', 'urban/transit', 'urban/industrial', 'urban/rooftop', 'urban/ruins', 'urban/plaza',
                'nature/grassland', 'nature/wheatfield', 'nature/flower_tree', 'nature/forest', 'nature/countryside', 'nature/park',
                'coastal/beach', 'coastal/harbor', 'coastal/shore',
                'traditional/chinese', 'traditional/japanese'
            ],
            source_type: ['mobile_game', 'galgame', 'anime', 'comic', 'original', 'vtuber', 'other'],
            reference_type: ['default', 'alternate', 'collab', 'official_art', 'merch', 'fan_art', 'custom', 'other'],
            collaboration_type: ['tf', 'photographer_paid', 'group_shoot', 'client_commissioned'],
            venue_fee_payer: ['model', 'photographer', 'split'],
            light_type: ['strobe', 'continuous', 'natural'],
            role: ['key', 'fill', 'separation', 'set'],
            modifier: [
                'standard reflector', 'deep parabolic softbox', 'snoot', 'Fresnel',
                '50cm octabox', '90cm deep parabolic softbox', '30×120cm strip softbox',
                '40×40cm softbox', '90cm octabox', '90cm deep parabolic umbrella',
                '105cm reflective umbrella', 'shoot-through umbrella', 'deep parabolic umbrella',
                'softbox', 'beauty dish', '120cm octabox', 'diffusion frame',
                '130cm reflective umbrella + diffusion fabric', '165cm reflective umbrella',
                'frosted blinds', 'reflective umbrella + diffusion frame', '100×100cm softbox',
                'octabox', 'strip softbox', 'octabox + diffusion frame', 'rectangular softbox',
                'NANLUX FE30 Parallel Beam Reflector + 25×25cm reflector panel',
                'Godox P88 parabolic reflector', 'Nanlite PJ-FMM Projection Attachment',
                'bare bulb', 'parallel reflector', 'honeycomb grid', 'optical snoot',
                '55cm octabox', 'venetian blinds'
            ],
            position: [
                'front', 'overhead', 'high_rear_right', 'high_rear_left', 'high_rear_side',
                'rear', 'rear_right', 'rear_left', 'rear_side',
                'front_right', 'front_left', 'front_side', 'side_right', 'side_left'
            ]
        };

        const showLibraryShareDialog = ref(false);
        const libraryShareForm = ref({title: '', password: '', allow_select: true});

        const currentShareToken = ref('');
        const libraryShare = ref({images: []});
        const shareNeedsPassword = ref(false);
        const sharePassword = ref('');
        const shareLoading = ref(false);

        const manifestArrayVisible = ref(false);
        const manifestArrayLoading = ref(false);
        const manifestArrayText = ref('');
        const manifestArrayCount = ref(0);

        const setSortOrderStorageKey = 'photo_library.setSortOrder';
        let savedSetSortOrder = 'newest';
        try {
            const storedOrder = window.localStorage.getItem(setSortOrderStorageKey);
            if (storedOrder === 'newest' || storedOrder === 'oldest') savedSetSortOrder = storedOrder;
        } catch (_) {
            // localStorage 不可用时保持默认排序，不影响目录浏览。
        }
        const setSortOrder = ref(savedSetSortOrder);
        watch(setSortOrder, (order) => {
            if (order !== 'newest' && order !== 'oldest') return;
            try {
                window.localStorage.setItem(setSortOrderStorageKey, order);
            } catch (_) {
                // 持久化失败只影响偏好记忆。
            }
        });

        const setDirectoryDate = (item) => {
            const match = String(item && item.name || '').match(/^(\d{8})-/);
            return match ? Number(match[1]) : null;
        };

        // Set root search is intentionally local-only: it filters the directories
        // already returned for the current root and never walks nested folders.
        const setSearchQuery = ref('');
        const normalizeSetSearchText = (value) => String(value || '')
            .normalize('NFKC')
            .toLocaleLowerCase();
        const setSearchTerms = computed(() => normalizeSetSearchText(setSearchQuery.value)
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .map(term => term.replace(/[\s_\-–—·・.()[\]（）]+/g, '')));

        const allLibraryDirectories = computed(() => {
            if (currentView.value === 'smart-set') return smartSetDirectoryItems.value;
            return (libraryListing.value.items || []).filter(item => item.type === 'directory');
        });

        const libraryDirectories = computed(() => {
            let directories = allLibraryDirectories.value;
            // Smart Set reuses the exact Set-parent display controls; nested physical
            // workflow folders retain their filesystem/API order.
            if (currentView.value !== 'smart-set' && (libraryListing.value.path || '') !== '') return directories;

            if (setSearchTerms.value.length > 0) {
                directories = directories.filter(item => {
                    const haystack = normalizeSetSearchText(item && item.name)
                        .replace(/[\s_\-–—·・.()[\]（）]+/g, '');
                    return setSearchTerms.value.every(term => haystack.includes(term));
                });
            }

            return [...directories].sort((a, b) => {
                const aDate = setDirectoryDate(a);
                const bDate = setDirectoryDate(b);
                if (aDate !== null && bDate !== null && aDate !== bDate) {
                    return setSortOrder.value === 'oldest' ? aDate - bDate : bDate - aDate;
                }
                if (aDate !== null && bDate === null) return -1;
                if (aDate === null && bDate !== null) return 1;
                return String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN', {numeric: true, sensitivity: 'base'});
            });
        });

        const newSetModelOptions = computed(() => {
            const models = new Set();
            const addModel = (value) => {
                const model = String(value || '').trim();
                if (model) models.add(model);
            };

            // Keep current-Source folder candidates so Sets without a manifest
            // still contribute, then merge cross-Source manifest references.
            for (const item of allLibraryDirectories.value) {
                let model = String(item && item.manifest_model || '').trim();
                if (!model) {
                    const match = String(item && item.name || '').match(/^\d{8}-([^-]+)-/);
                    model = match ? match[1].trim() : '';
                }
                addModel(model);
            }
            for (const model of (manifestKnownValues.value?.models || [])) {
                addModel(model);
            }

            return Array.from(models).sort((a, b) => a.localeCompare(b, 'zh-CN', {numeric: true, sensitivity: 'base'}));
        });

        const libraryImages = computed(() => (libraryListing.value.items || [])
            .filter(item => item.type === 'image')
            .map(item => ({
                ...item,
                source_type: 'library',
                source_id: currentLibrarySource.value ? currentLibrarySource.value.id : null,
                id: `library:${currentLibrarySource.value ? currentLibrarySource.value.id : ''}:${item.relative_path}`,
                original_filename: item.name,
                file_size: item.size,
                uploaded_at: item.modified_at,
                description: item.description || '',
                is_favorited: !!item.is_favorited
            })));
        const shareDirectories = computed(() => (libraryShare.value.listing?.items || [])
            .filter(item => item.type === 'directory'));
        const shareImages = computed(() => (libraryShare.value.listing?.items || [])
            .filter(item => item.type === 'image')
            .map(item => ({
                ...item,
                source_type: 'library-share',
                share_token: currentShareToken.value,
                id: `share:${currentShareToken.value}:${item.relative_path}`,
                original_filename: item.name,
                file_size: item.size,
                uploaded_at: item.modified_at,
                description: item.description || '',
                is_favorited: !!(item.is_favorited ?? item.selected)
            })));
        const shareEmptyMessage = computed(() => {
            const stats = libraryShare.value.listing?.stats || {};
            if (shareImages.value.length > 0) return '';
            if (shareDirectories.value.length > 0) return '当前层没有图片，请进入子文件夹查看。';
            if ((Number(stats.unsupported_file_count) || 0) > 0) {
                return `这个目录有 ${stats.unsupported_file_count} 个文件，但没有支持显示的图片。`;
            }
            return '这个目录是空的。';
        });
        const libraryStats = computed(() => libraryListing.value.stats || {
            directory_count: libraryDirectories.value.length,
            image_count: libraryImages.value.length,
            unsupported_file_count: 0,
            total_file_count: libraryImages.value.length
        });
        const libraryFavoriteCount = computed(() => libraryImages.value.filter(item => item.is_favorited).length);
        const libraryEmptyMessage = computed(() => {
            const stats = libraryStats.value;
            if (libraryImages.value.length > 0) return '';
            if ((libraryListing.value.path || '') === '' && setSearchTerms.value.length > 0 && allLibraryDirectories.value.length > 0 && libraryDirectories.value.length === 0) {
                return `没有匹配“${String(setSearchQuery.value || '').trim()}”的 Set。`;
            }
            if (allLibraryDirectories.value.length > 0) {
                if (stats.unsupported_file_count > 0) {
                    return `当前层没有可显示图片，请进入子文件夹；另有 ${stats.unsupported_file_count} 个不支持显示的文件。`;
                }
                return '当前层没有图片，请进入子文件夹查看。';
            }
            if (stats.unsupported_file_count > 0) {
                return `这个目录有 ${stats.unsupported_file_count} 个文件，但没有支持显示的图片。`;
            }
            return '这个目录是空的。';
        });
        const shareSelectedImages = computed(() => shareImages.value.filter(item => item.is_favorited));


        const isSetDirectory = computed(() => !!libraryListing.value.is_set);
        const isLibraryRoot = computed(() => !!currentLibrarySource.value && (libraryListing.value.path || '') === '');

        const localToday = () => {
            const now = new Date();
            const year = now.getFullYear();
            const month = String(now.getMonth() + 1).padStart(2, '0');
            const day = String(now.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        };

        const openNewSetDialog = async () => {
            if (!isLibraryRoot.value || !currentLibrarySource.value) {
                ElMessage.warning('New Set 只能在 Set 父目录创建');
                return;
            }
            newSetForm.value = {model: '', date: localToday(), theme: ''};
            showNewSetDialog.value = true;

            // New Set shares the same cross-Source model references as Manifest Autofill.
            await manifestAutofill.load();
            manifestKnownValues.value = manifestAutofill.getValues();
        };

        const createNewSet = async () => {
            if (!currentLibrarySource.value || !isLibraryRoot.value) {
                ElMessage.error('当前目录不是 Set 父目录');
                return;
            }

            const payload = {
                model: String(newSetForm.value.model || '').trim(),
                date: String(newSetForm.value.date || '').trim(),
                theme: String(newSetForm.value.theme || '').trim()
            };
            if (!payload.model || !payload.date || !payload.theme) {
                ElMessage.warning('模特、日期、主题都必须填写');
                return;
            }

            newSetCreating.value = true;
            try {
                const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/sets`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '创建 Set 失败');

                showNewSetDialog.value = false;
                await loadLibraryDirectory('');
                librarySelectedDirectoryPath.value = data.path || '';
                ElMessage.success(`已创建 ${data.name}`);
            } catch (error) {
                ElMessage.error(error?.message || '创建 Set 失败');
            } finally {
                newSetCreating.value = false;
            }
        };

        const newLightingRow = () => ({
            role: '',
            light_type: '',
            fixture: '',
            modifier: '',
            count: 1,
            position: '',
            note: ''
        });

        // Only known manifest fields have a canonical order. Custom fields
        // (for example a future `project`) are preserved in the exact slot
        // where the user inserted them. Missing optional fields are not added.
        const manifestKeyOrders = {
            root: ['model', 'shoot', 'location', 'theme', 'production', 'props', 'lighting'],
            shoot: ['date', 'start_time', 'end_time', 'environment', 'scene', 'weather', 'additional_sessions'],
            additional_sessions: ['date', 'start_time', 'end_time', 'weather'],
            location: ['name', 'address', 'lat', 'lng'],
            theme: ['name', 'genre', 'source_title', 'source_type', 'character', 'variant', 'reference_type', 'reference', 'outfit'],
            // Optional credits stay at the end when they exist.
            production: ['collaboration_type', 'lead_photographer', 'model_fee', 'venue_fee', 'venue_fee_payer', 'primary_photographer', 'assistants'],
            props: ['subject', 'set'],
            lighting: ['role', 'light_type', 'fixture', 'modifier', 'count', 'position', 'note']
        };

        const orderKnownKeysPreservingUnknownPositions = (value, preferredOrder) => {
            if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
            const preferred = preferredOrder.filter(key => Object.prototype.hasOwnProperty.call(value, key));
            const preferredSet = new Set(preferredOrder);
            let preferredIndex = 0;
            const result = {};
            Object.keys(value).forEach(key => {
                if (preferredSet.has(key)) {
                    const orderedKey = preferred[preferredIndex++];
                    result[orderedKey] = value[orderedKey];
                } else {
                    result[key] = value[key];
                }
            });
            return result;
        };

        const orderManifestKeys = (payload) => {
            if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return payload;
            const result = {...payload};

            if (result.shoot && typeof result.shoot === 'object' && !Array.isArray(result.shoot)) {
                const shoot = {...result.shoot};
                if (Array.isArray(shoot.additional_sessions)) {
                    shoot.additional_sessions = shoot.additional_sessions.map(item => (
                        item && typeof item === 'object' && !Array.isArray(item)
                            ? orderKnownKeysPreservingUnknownPositions(item, manifestKeyOrders.additional_sessions)
                            : item
                    ));
                }
                result.shoot = orderKnownKeysPreservingUnknownPositions(shoot, manifestKeyOrders.shoot);
            }

            ['location', 'theme', 'production', 'props'].forEach(section => {
                if (result[section] && typeof result[section] === 'object' && !Array.isArray(result[section])) {
                    result[section] = orderKnownKeysPreservingUnknownPositions(result[section], manifestKeyOrders[section]);
                }
            });

            if (Array.isArray(result.lighting)) {
                result.lighting = result.lighting.map(item => (
                    item && typeof item === 'object' && !Array.isArray(item)
                        ? orderKnownKeysPreservingUnknownPositions(item, manifestKeyOrders.lighting)
                        : item
                ));
            }

            return orderKnownKeysPreservingUnknownPositions(result, manifestKeyOrders.root);
        };

        const normalizeManifest = (raw = {}) => {
            const data = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
            const shoot = data.shoot && typeof data.shoot === 'object' && !Array.isArray(data.shoot) ? data.shoot : {};
            const location = data.location && typeof data.location === 'object' && !Array.isArray(data.location) ? data.location : {};
            const theme = data.theme && typeof data.theme === 'object' && !Array.isArray(data.theme) ? data.theme : {};
            const production = data.production && typeof data.production === 'object' && !Array.isArray(data.production) ? data.production : {};
            const props = data.props && typeof data.props === 'object' && !Array.isArray(data.props) ? data.props : {};
            const lights = Array.isArray(data.lighting) ? data.lighting : [];
            const additionalSessions = Array.isArray(shoot.additional_sessions)
                ? shoot.additional_sessions.filter(item => item && typeof item === 'object' && !Array.isArray(item))
                : [];

            return {
                model: data.model ?? '',
                shoot: {
                    date: shoot.date ?? '',
                    start_time: shoot.start_time ?? '',
                    end_time: shoot.end_time ?? '',
                    environment: shoot.environment ?? '',
                    scene: shoot.scene ?? '',
                    weather: shoot.weather ?? '',
                    additional_sessions: additionalSessions.map(item => ({
                        date: item.date ?? '',
                        start_time: item.start_time ?? '',
                        end_time: item.end_time ?? '',
                        weather: item.weather ?? ''
                    }))
                },
                location: {
                    name: location.name ?? '',
                    address: location.address ?? '',
                    lat: location.lat ?? null,
                    lng: location.lng ?? null
                },
                theme: {
                    name: theme.name ?? '',
                    genre: theme.genre ?? '',
                    source_title: theme.source_title ?? '',
                    source_type: theme.source_type ?? '',
                    character: theme.character ?? '',
                    variant: theme.variant ?? '',
                    reference_type: theme.reference_type ?? '',
                    reference: theme.reference ?? '',
                    outfit: theme.outfit ?? ''
                },
                production: {
                    collaboration_type: String(production.collaboration_type ?? '').trim() || 'tf',
                    lead_photographer: production.lead_photographer ?? true,
                    primary_photographer: production.primary_photographer ?? '',
                    assistants: Array.isArray(production.assistants) ? [...production.assistants] : [],
                    model_fee: production.model_fee ?? 0,
                    venue_fee: production.venue_fee !== undefined ? production.venue_fee : null,
                    venue_fee_payer: production.venue_fee_payer ?? ''
                },
                props: {
                    subject: Array.isArray(props.subject) ? [...props.subject] : [],
                    set: Array.isArray(props.set) ? [...props.set] : []
                },
                lighting: lights.map(light => ({
                    role: light?.role ?? '',
                    light_type: light?.light_type ?? '',
                    fixture: light?.fixture ?? '',
                    modifier: light?.modifier ?? '',
                    count: light?.count ?? 1,
                    position: light?.position ?? '',
                    note: light?.note ?? ''
                }))
            };
        };

        const applyManifestCommonStorageRules = (payload) => {
            if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return payload;

            let production = payload.production;
            if (production && typeof production === 'object' && !Array.isArray(production)) {
                production = {...production};
                if (Object.prototype.hasOwnProperty.call(production, 'primary_photographer')) {
                    const primaryPhotographer = typeof production.primary_photographer === 'string'
                        ? production.primary_photographer.trim()
                        : production.primary_photographer;
                    if (primaryPhotographer === '' || primaryPhotographer === null || primaryPhotographer === undefined) {
                        delete production.primary_photographer;
                    } else {
                        production.primary_photographer = primaryPhotographer;
                    }
                }
            }

            return orderManifestKeys({
                ...payload,
                ...(production && typeof production === 'object' && !Array.isArray(production) ? {production} : {})
            });
        };

        const applyManifestStorageRules = (payload) => {
            payload = applyManifestCommonStorageRules(payload);
            if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return payload;

            const theme = payload.theme && typeof payload.theme === 'object' && !Array.isArray(payload.theme)
                ? {...payload.theme}
                : {};
            const genre = theme.genre ?? '';

            if (genre === 'cosplay') {
                delete theme.outfit;
                theme.source_title = theme.source_title ?? '';
                theme.source_type = theme.source_type ?? '';
                theme.character = theme.character ?? '';
                theme.variant = theme.variant ?? '';
                theme.reference_type = theme.reference_type ?? '';
                theme.reference = theme.reference ?? '';
            } else {
                delete theme.source_title;
                delete theme.source_type;
                delete theme.character;
                delete theme.variant;
                delete theme.reference_type;
                delete theme.reference;
                theme.outfit = theme.outfit ?? '';
            }

            const lighting = Array.isArray(payload.lighting)
                ? payload.lighting.map(light => {
                    const item = light && typeof light === 'object' && !Array.isArray(light) ? light : {};
                    return orderKnownKeysPreservingUnknownPositions({
                        ...item,
                        role: item.role ?? '',
                        light_type: item.light_type ?? '',
                        fixture: item.fixture ?? '',
                        modifier: item.modifier ?? '',
                        count: item.count ?? 1,
                        position: item.position ?? '',
                        note: item.note ?? ''
                    }, manifestKeyOrders.lighting);
                })
                : payload.lighting;

            return orderManifestKeys({
                ...payload,
                theme,
                ...(Array.isArray(lighting) ? {lighting} : {})
            });
        };

        const trimManifestFormStrings = (value) => {
            if (typeof value === 'string') return value.trim();
            if (Array.isArray(value)) return value.map(item => trimManifestFormStrings(item));
            if (value && typeof value === 'object') {
                return Object.fromEntries(
                    Object.entries(value).map(([key, item]) => [key, trimManifestFormStrings(item)])
                );
            }
            return value;
        };

        const serializeManifestForm = () => {
            const form = normalizeManifest(manifestForm.value);
            const shoot = {
                date: form.shoot.date,
                start_time: form.shoot.start_time,
                end_time: form.shoot.end_time,
                environment: form.shoot.environment,
                scene: form.shoot.scene,
                weather: form.shoot.weather
            };
            if (form.shoot.additional_sessions.length) {
                shoot.additional_sessions = form.shoot.additional_sessions.map(item => ({...item}));
            }

            const theme = {
                name: form.theme.name,
                genre: form.theme.genre
            };
            if (form.theme.genre === 'cosplay') {
                theme.source_title = form.theme.source_title;
                theme.source_type = form.theme.source_type;
                theme.character = form.theme.character;
                theme.variant = form.theme.variant;
                theme.reference_type = form.theme.reference_type;
                theme.reference = form.theme.reference;
            } else {
                theme.outfit = form.theme.outfit;
            }

            const production = {
                collaboration_type: form.production.collaboration_type,
                lead_photographer: form.production.lead_photographer,
                model_fee: form.production.model_fee,
                venue_fee: form.production.venue_fee,
                venue_fee_payer: form.production.venue_fee_payer
            };
            if (form.production.lead_photographer) {
                if (form.production.assistants.length) {
                    production.assistants = [...form.production.assistants];
                }
            } else if (form.production.primary_photographer) {
                production.primary_photographer = form.production.primary_photographer;
            }

            return {
                model: form.model,
                shoot,
                location: {...form.location},
                theme,
                production,
                props: {
                    subject: [...form.props.subject],
                    set: [...form.props.set]
                },
                lighting: form.lighting.map(light => ({
                    role: light.role,
                    light_type: light.light_type,
                    fixture: light.fixture,
                    modifier: light.modifier,
                    count: light.count,
                    position: light.position,
                    note: light.note
                }))
            };
        };

        const parseManifestJsonText = () => {
            try {
                const parsed = JSON.parse(manifestJsonText.value || '');
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                    throw new Error('manifest root must be a JSON object');
                }
                manifestJsonError.value = '';
                return parsed;
            } catch (error) {
                manifestJsonError.value = error && error.message ? error.message : 'Invalid JSON';
                return null;
            }
        };

        const mergeManifestFormIntoRaw = (raw, formPayload) => {
            const source = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
            const merged = {...source};

            merged.model = formPayload.model;
            const nextShoot = {
                ...(source.shoot && typeof source.shoot === 'object' && !Array.isArray(source.shoot) ? source.shoot : {}),
                ...formPayload.shoot
            };
            if (!Object.prototype.hasOwnProperty.call(formPayload.shoot, 'additional_sessions')) {
                delete nextShoot.additional_sessions;
            }
            merged.shoot = nextShoot;
            merged.location = {
                ...(source.location && typeof source.location === 'object' && !Array.isArray(source.location) ? source.location : {}),
                ...formPayload.location
            };

            const sourceTheme = source.theme && typeof source.theme === 'object' && !Array.isArray(source.theme) ? source.theme : {};
            const nextTheme = {...sourceTheme, name: formPayload.theme.name, genre: formPayload.theme.genre};
            if (formPayload.theme.genre === 'cosplay') {
                nextTheme.source_title = formPayload.theme.source_title ?? '';
                nextTheme.source_type = formPayload.theme.source_type ?? '';
                nextTheme.character = formPayload.theme.character ?? '';
                nextTheme.variant = formPayload.theme.variant ?? '';
                nextTheme.reference_type = formPayload.theme.reference_type ?? '';
                nextTheme.reference = formPayload.theme.reference ?? '';
                delete nextTheme.outfit;
            } else {
                nextTheme.outfit = formPayload.theme.outfit ?? '';
                delete nextTheme.source_title;
                delete nextTheme.source_type;
                delete nextTheme.character;
                delete nextTheme.variant;
                delete nextTheme.reference_type;
                delete nextTheme.reference;
            }
            merged.theme = nextTheme;

            const nextProduction = {
                ...(source.production && typeof source.production === 'object' && !Array.isArray(source.production) ? source.production : {}),
                ...formPayload.production
            };
            if (formPayload.production.lead_photographer) {
                delete nextProduction.primary_photographer;
                if (!Array.isArray(formPayload.production.assistants) || !formPayload.production.assistants.length) {
                    delete nextProduction.assistants;
                }
            } else {
                delete nextProduction.assistants;
                if (!formPayload.production.primary_photographer) {
                    delete nextProduction.primary_photographer;
                }
            }
            merged.production = nextProduction;
            merged.props = {
                ...(source.props && typeof source.props === 'object' && !Array.isArray(source.props) ? source.props : {}),
                subject: [...formPayload.props.subject],
                set: [...formPayload.props.set]
            };

            const sourceLights = Array.isArray(source.lighting) ? source.lighting : [];
            merged.lighting = formPayload.lighting.map((light, index) => ({
                ...(sourceLights[index] && typeof sourceLights[index] === 'object' && !Array.isArray(sourceLights[index]) ? sourceLights[index] : {}),
                role: light.role,
                light_type: light.light_type,
                fixture: light.fixture,
                modifier: light.modifier,
                count: light.count,
                position: light.position,
                note: light.note
            }));

            return orderManifestKeys(merged);
        };

        const syncManifestJsonFromForm = () => {
            const currentRaw = parseManifestJsonText();
            if (!currentRaw) {
                ElMessage.error('当前 JSON 格式有误，不能安全地从 Form 同步');
                return;
            }
            const formPayload = applyManifestStorageRules(trimManifestFormStrings(serializeManifestForm()));
            const merged = mergeManifestFormIntoRaw(currentRaw, formPayload);
            manifestJsonText.value = JSON.stringify(merged, null, 2);
            manifestJsonError.value = '';
        };

        const syncManifestFormFromJson = () => {
            const parsed = parseManifestJsonText();
            if (!parsed) {
                ElMessage.error('JSON 格式有误，无法同步到 Form');
                return;
            }
            manifestForm.value = normalizeManifest(parsed);
        };

        const switchManifestEditorMode = (mode) => {
            if (mode === manifestEditorMode.value) return;
            manifestEditorMode.value = mode;
        };

        const formatManifestJson = () => {
            const parsed = parseManifestJsonText();
            if (!parsed) return;
            manifestJsonText.value = JSON.stringify(orderManifestKeys(parsed), null, 2);
        };

        const currentManifestData = computed(() => {
            if (currentView.value === 'library-share') {
                const manifest = libraryShare.value.manifest;
                return manifest && typeof manifest === 'object' ? normalizeManifest(manifest) : null;
            }
            const manifest = libraryListing.value.manifest || {};
            return manifest.valid && manifest.data ? normalizeManifest(manifest.data) : null;
        });

        const formatEnumValue = (value) => {
            if (value === null || value === undefined || value === '') return '—';
            if (value === 'tf') return 'TF';
            return String(value)
                .split('_')
                .filter(Boolean)
                .map(part => part.charAt(0).toUpperCase() + part.slice(1))
                .join(' ');
        };

        const formatManifestValue = (value) => {
            if (value === null || value === undefined || value === '') return '—';
            if (typeof value === 'boolean') return value ? 'Yes' : 'No';
            if (Array.isArray(value)) return value.length ? value.join(' · ') : '—';
            return String(value);
        };

        // Detail preview visibility only. These helpers never alter Form or Raw JSON.
        const hasManifestValue = (value) => {
            if (value === null || value === undefined) return false;
            if (typeof value === 'string') return value.trim() !== '';
            return true;
        };

        const hasManifestProps = computed(() => {
            const data = currentManifestData.value;
            if (!data || !data.props) return false;
            const subject = Array.isArray(data.props.subject) ? data.props.subject : [];
            const set = Array.isArray(data.props.set) ? data.props.set : [];
            return subject.length > 0 || set.length > 0;
        });

        const formatSceneValue = (value) => {
            if (value === null || value === undefined || value === '') return '—';
            return String(value).split('/').map(part => formatEnumValue(part)).join(' › ');
        };


        // Detail-only computed fields. These are deliberately not part of the
        // manifest editor/serializer and never get written back to manifest.json.
        const parseClockMinutes = (value) => {
            const match = String(value || '').trim().match(/^(\d{1,2}):(\d{2})$/);
            if (!match) return null;
            const hours = Number(match[1]);
            const minutes = Number(match[2]);
            if (!Number.isInteger(hours) || !Number.isInteger(minutes) || hours < 0 || hours > 23 || minutes < 0 || minutes > 59) {
                return null;
            }
            return hours * 60 + minutes;
        };

        const manifestShootDuration = computed(() => {
            const data = currentManifestData.value;
            if (!data) return '';
            const start = parseClockMinutes(data.shoot.start_time);
            const end = parseClockMinutes(data.shoot.end_time);
            if (start === null || end === null) return '';

            let duration = end - start;
            // If a shoot crosses midnight, treat the end time as the next day.
            if (duration < 0) duration += 24 * 60;

            const hours = Math.floor(duration / 60);
            const minutes = duration % 60;
            if (hours && minutes) return `${hours}h ${minutes}m`;
            if (hours) return `${hours}h`;
            return `${minutes}m`;
        });

        const currentManifestLightCount = computed(() => {
            const data = currentManifestData.value;
            if (!data || !Array.isArray(data.lighting)) return 0;
            return data.lighting.reduce((total, light) => {
                const count = Number(light && light.count);
                return total + (Number.isFinite(count) && count > 0 ? count : 1);
            }, 0);
        });

        const manifestVenuePaid = computed(() => {
            const data = currentManifestData.value;
            if (!data) return null;
            const production = data.production || {};
            const fee = Number(production.venue_fee);
            if (!Number.isFinite(fee) || fee <= 0) return null;

            if (production.venue_fee_payer === 'photographer') return fee;
            if (production.venue_fee_payer === 'split') return fee / 2;
            if (production.venue_fee_payer === 'model') return 0;
            return null;
        });

        const formatWeatherValue = (value) => {
            if (value === null || value === undefined || value === '') return '—';
            const key = String(value).trim().toLowerCase();
            const icons = {
                sunny: '☀️',
                cloudy: '⛅️',
                overcast: '☁️',
                rainy: '🌧️',
                snowy: '🌨️'
            };
            return icons[key] ? `${icons[key]} ${formatEnumValue(value)}` : formatEnumValue(value);
        };

        const formatLibraryFolderCounts = (item) => {
            if (!item) return '';
            const directoryCount = Number(item.directory_count) || 0;
            const imageCount = Number(item.image_count) || 0;
            const fileCount = Number(item.file_count) || 0;
            const parts = [];

            if (directoryCount > 0) {
                parts.push(`${directoryCount} 目录`);
            }

            if (fileCount > 0) {
                if (imageCount > 0 && fileCount === imageCount) {
                    parts.push(`${imageCount} 图片`);
                } else if (imageCount > 0) {
                    parts.push(`${fileCount} 文件（含 ${imageCount} 图片）`);
                } else {
                    parts.push(`${fileCount} 文件`);
                }
            } else if (imageCount > 0) {
                // Defensive fallback for older API payloads where file_count was absent.
                parts.push(`${imageCount} 图片`);
            }

            return parts.length > 0 ? parts.join(' · ') : 'empty';
        };

        const addLightingRow = () => {
            if (!Array.isArray(manifestForm.value.lighting)) manifestForm.value.lighting = [];
            manifestForm.value.lighting.push(newLightingRow());
        };

        const removeLightingRow = (index) => {
            if (!Array.isArray(manifestForm.value.lighting)) return;
            manifestForm.value.lighting.splice(index, 1);
        };

        const addAdditionalSession = () => {
            if (!manifestForm.value.shoot) manifestForm.value.shoot = {};
            if (!Array.isArray(manifestForm.value.shoot.additional_sessions)) {
                manifestForm.value.shoot.additional_sessions = [];
            }
            manifestForm.value.shoot.additional_sessions.push({
                date: '',
                start_time: '',
                end_time: '',
                weather: ''
            });
        };

        const removeAdditionalSession = (index) => {
            if (!manifestForm.value.shoot || !Array.isArray(manifestForm.value.shoot.additional_sessions)) return;
            manifestForm.value.shoot.additional_sessions.splice(index, 1);
        };

        const loadLibrarySources = async () => {
            if (!isAdmin.value) {
                librarySources.value = [];
                return;
            }
            try {
                const response = await fetch('/api/library/sources');
                if (response.ok) {
                    librarySources.value = await response.json();
                }
            } catch (error) {
                console.error('加载本地目录失败:', error);
            }
        };

        const addLibrarySource = async () => {
            if (!newLibrarySource.value.name.trim() || !newLibrarySource.value.root_path.trim()) {
                ElMessage.warning('请输入名称和本地绝对路径');
                return;
            }
            try {
                const response = await fetch('/api/library/sources', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(newLibrarySource.value)
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '添加 Source 失败');
                    return;
                }
                ElMessage.success('本地目录已添加');
                newLibrarySource.value = {name: 'Completed', root_path: ''};
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
            } catch (error) {
                ElMessage.error('添加 Source 失败');
            }
        };

        const renameLibrarySource = async (source) => {
            try {
                const {value} = await ElMessageBox.prompt('请输入新的 Source 名称', '重命名 Source', {
                    confirmButtonText: '保存',
                    cancelButtonText: '取消',
                    inputValue: source.name,
                    inputPattern: /\S+/,
                    inputErrorMessage: '名称不能为空'
                });
                const name = String(value || '').trim();
                if (!name || name === source.name) return;

                const response = await fetch(`/api/library/sources/${source.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '重命名失败');
                    return;
                }
                if (currentLibrarySource.value && currentLibrarySource.value.id === source.id) {
                    currentLibrarySource.value = {...currentLibrarySource.value, name};
                }
                await loadLibrarySources();
                ElMessage.success('Source 已重命名');
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('重命名失败');
            }
        };

        const deleteLibrarySource = async (source) => {
            try {
                await ElMessageBox.confirm(`只删除映射，不会删除硬盘文件。确定移除 “${source.name}” 吗？`, '移除 Source', {
                    confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning'
                });
                const response = await fetch(`/api/library/sources/${source.id}`, {method: 'DELETE'});
                if (!response.ok) {
                    const data = await response.json();
                    ElMessage.error(data.error || '移除失败');
                    return;
                }
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
                ElMessage.success('映射已移除，硬盘文件未修改');
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('移除失败');
            }
        };

        const setLibrarySourceEnabled = async (source, enabled) => {
            const action = enabled ? '启用' : '停用';
            try {
                if (!enabled) {
                    await ElMessageBox.confirm(
                        `停用 “${source.name}” 后将无法进入，也不会参与 Autofill 等 Source 功能；已有数据库状态会保留。确定停用吗？`,
                        '停用 Source',
                        {confirmButtonText: '停用', cancelButtonText: '取消', type: 'warning'}
                    );
                }
                const response = await fetch(`/api/library/sources/${source.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || `${action}失败`);
                    return;
                }
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
                ElMessage.success(`Source 已${action}`);
            } catch (error) {
                if (error !== 'cancel') ElMessage.error(`${action}失败`);
            }
        };

        const libraryDirectoryStateKey = (path = '') =>
            `${currentLibrarySource.value ? currentLibrarySource.value.id : ''}:${path || ''}`;

        const rememberLibraryDirectoryPosition = (item, event) => {
            if (!item) return;
            const parentPath = (libraryListing.value && libraryListing.value.path) || '';
            const target = event && event.currentTarget instanceof Element ? event.currentTarget : null;
            libraryDirectoryNavigationState.set(libraryDirectoryStateKey(parentPath), {
                selectedPath: item.relative_path || '',
                scrollY: window.scrollY || window.pageYOffset || 0,
                viewportTop: target ? target.getBoundingClientRect().top : null
            });
            librarySelectedDirectoryPath.value = item.relative_path || '';
        };

        const restoreLibraryDirectoryPosition = async (path = '') => {
            const state = libraryDirectoryNavigationState.get(libraryDirectoryStateKey(path));
            librarySelectedDirectoryPath.value = state && state.selectedPath ? state.selectedPath : '';
            if (!state) return;

            await nextTick();
            window.requestAnimationFrame(() => {
                const nodes = document.querySelectorAll('[data-library-directory-path]');
                const target = Array.from(nodes).find(node =>
                    node.getAttribute('data-library-directory-path') === state.selectedPath
                );

                if (target && Number.isFinite(state.viewportTop)) {
                    const delta = target.getBoundingClientRect().top - state.viewportTop;
                    window.scrollTo(0, Math.max(0, (window.scrollY || window.pageYOffset || 0) + delta));
                } else if (Number.isFinite(state.scrollY)) {
                    window.scrollTo(0, Math.max(0, state.scrollY));
                }
            });
        };

        const scrollLibraryPageTop = async () => {
            await nextTick();
            window.requestAnimationFrame(() => window.scrollTo(0, 0));
        };

        const loadLibraryDirectory = async (path = '') => {
            if (!currentLibrarySource.value) return;
            libraryLoading.value = true;
            try {
                const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/browse?path=${encodeURIComponent(path || '')}`);
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '目录读取失败');
                    return;
                }
                libraryListing.value = data;
                currentView.value = 'library';
                const params = new URLSearchParams();
                params.set('source', currentLibrarySource.value.id);
                if (data.path) params.set('path', data.path);
                window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`);
            } catch (error) {
                ElMessage.error('目录读取失败');
            } finally {
                libraryLoading.value = false;
            }
        };

        const openLibrarySource = async (source, path = '') => {
            // A normal Library entry owns its physical parent chain. Smart Set
            // navigation sets a fresh context only after this open succeeds.
            smartSetEntryContext = null;
            if (!source.enabled) {
                ElMessage.warning(`Source 已停用: ${source.name}`);
                return;
            }
            if (!source.available) {
                ElMessage.error(`本地目录不可用: ${source.root_path}`);
                return;
            }
            currentLibrarySource.value = source;
            await loadLibraryDirectory(path);
        };

        const openLibraryDirectory = async (item, event = null) => {
            rememberLibraryDirectoryPosition(item, event);
            await loadLibraryDirectory(item.relative_path);
            librarySelectedDirectoryPath.value = '';
            await scrollLibraryPageTop();
        };

        const openSetParentDirectory = async (item, event = null) => {
            if (currentView.value === 'smart-set') {
                await openSmartSetResult(item, event);
                return;
            }
            await openLibraryDirectory(item, event);
        };

        const jumpToRelatedSet = async (path) => {
            if (!path) return;
            showManifestDetailDialog.value = false;
            await loadLibraryDirectory(path);
            librarySelectedDirectoryPath.value = '';
            await scrollLibraryPageTop();
        };

        const libraryBack = async () => {
            const context = smartSetEntryContext;
            const currentSourceId = Number(currentLibrarySource.value && currentLibrarySource.value.id);
            const currentPath = String(libraryListing.value && libraryListing.value.path || '');
            const canReturnToSmartSet = Boolean(
                context
                && smartSetContextKey(currentSmartSet.value) === context.smartSetKey
                && currentSourceId === context.sourceId
                && currentPath === context.setPath
            );

            if (canReturnToSmartSet) {
                smartSetEntryContext = null;
                currentView.value = 'smart-set';
                currentLibrarySource.value = null;
                libraryListing.value = {items: [], manifest: {exists: false}};
                librarySelectedDirectoryPath.value = '';
                updateUrlWithoutParams();

                await nextTick();
                window.requestAnimationFrame(() => {
                    const nodes = document.querySelectorAll('[data-library-directory-path]');
                    const target = Array.from(nodes).find(node =>
                        node.getAttribute('data-library-directory-path') === context.setPath
                    );
                    if (target && Number.isFinite(context.viewportTop)) {
                        const delta = target.getBoundingClientRect().top - context.viewportTop;
                        window.scrollTo(0, Math.max(0, (window.scrollY || window.pageYOffset || 0) + delta));
                    } else if (Number.isFinite(context.scrollY)) {
                        window.scrollTo(0, Math.max(0, context.scrollY));
                    }
                });
                return;
            }

            if (libraryListing.value.parent_path !== null && libraryListing.value.parent_path !== undefined) {
                const parentPath = libraryListing.value.parent_path || '';
                await loadLibraryDirectory(parentPath);
                await restoreLibraryDirectoryPosition(parentPath);
            } else {
                smartSetEntryContext = null;
                currentView.value = 'albums';
                currentLibrarySource.value = null;
                libraryListing.value = {items: [], manifest: {exists: false}};
                updateUrlWithoutParams();
            }
        };

        const libraryPathAssetUrl = (relativePath, variant = 'thumbnail') => {
            if (!currentLibrarySource.value || !relativePath) return '';
            return `/api/library/sources/${currentLibrarySource.value.id}/asset?variant=${encodeURIComponent(variant)}&path=${encodeURIComponent(relativePath)}`;
        };

        const libraryDirectoryCoverUrl = (item, variant = 'thumbnail') => {
            if (!item || !item.cover_path) return '';
            const sourceId = item.source_id || (currentLibrarySource.value && currentLibrarySource.value.id);
            if (!sourceId) return '';
            return `/api/library/sources/${sourceId}/asset?variant=${encodeURIComponent(variant)}&path=${encodeURIComponent(item.cover_path)}`;
        };

        const libraryAssetUrl = (item, variant = 'thumbnail') => {
            if (!item) return '';
            return libraryPathAssetUrl(item.relative_path, variant);
        };

        const detailImageUrl = (image, variant = 'compressed') => {
            if (!image) return '';
            if (image.source_type === 'library') {
                return `/api/library/sources/${image.source_id}/asset?variant=${encodeURIComponent(variant)}&path=${encodeURIComponent(image.relative_path)}`;
            }
            if (image.source_type === 'library-share') {
                return `/api/library/shares/${encodeURIComponent(image.share_token || currentShareToken.value)}/asset?variant=${encodeURIComponent(variant)}&path=${encodeURIComponent(image.relative_path)}`;
            }
            return `/api/images/${image.id}/file?type=${encodeURIComponent(variant)}`;
        };

        // Detail navigation must not wait for image-info before switching images.
        // The directory listing already contains enough information to render the next
        // image immediately; width/height and persisted state are hydrated afterwards.
        const detailImageInfoCache = new Map();
        const detailAssetPrefetches = new Set();

        const libraryImageInfoUrl = (item) =>
            `/api/library/sources/${item.source_id || (currentLibrarySource.value && currentLibrarySource.value.id)}/image-info?path=${encodeURIComponent(item.relative_path)}`;

        const sharedImageInfoUrl = (item) =>
            `/api/library/shares/${encodeURIComponent(item.share_token || currentShareToken.value)}/image-info?path=${encodeURIComponent(item.relative_path)}`;

        const cacheImageInfo = (data) => {
            if (data && data.id) detailImageInfoCache.set(data.id, data);
            return data;
        };

        const fetchImageInfo = async (item) => {
            if (!item || !item.id) return null;
            if (detailImageInfoCache.has(item.id)) return detailImageInfoCache.get(item.id);

            const url = item.source_type === 'library-share'
                ? sharedImageInfoUrl(item)
                : libraryImageInfoUrl(item);
            const response = await fetch(url);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '读取图片信息失败');
            return cacheImageInfo(data);
        };

        const updateLibraryListingState = (data) => {
            if (!data || data.source_type !== 'library') return;
            const index = (libraryListing.value.items || []).findIndex(i => i.relative_path === data.relative_path);
            if (index !== -1) {
                libraryListing.value.items[index] = {
                    ...libraryListing.value.items[index],
                    is_favorited: data.is_favorited,
                    description: data.description
                };
            }
            const smartIndex = smartAlbumImages.value.findIndex(i => i.id === data.id);
            if (smartIndex !== -1) {
                smartAlbumImages.value[smartIndex] = {
                    ...smartAlbumImages.value[smartIndex],
                    is_favorited: data.is_favorited,
                    description: data.description
                };
            }
        };

        const hydrateCurrentImage = async (item) => {
            const expectedId = item && item.id;
            if (!expectedId) return;
            try {
                const data = await fetchImageInfo(item);
                updateLibraryListingState(data);
                if (currentImage.value && currentImage.value.id === expectedId) {
                    currentImage.value = {...currentImage.value, ...data};
                }
            } catch (error) {
                console.warn('读取图片信息失败:', error);
                // The image itself can still be displayed from the directory listing.
            }
        };

        const prefetchDetailAsset = (item) => {
            if (!item || (item.source_type !== 'library' && item.source_type !== 'library-share')) return;
            const url = detailImageUrl(item, 'compressed');
            if (!url || detailAssetPrefetches.has(url)) return;
            detailAssetPrefetches.add(url);
            const image = new Image();
            image.decoding = 'async';
            image.src = url;
            void fetchImageInfo(item).catch(() => {});
        };

        const scheduleAdjacentDetailPrefetch = (expectedId) => {
            nextTick(() => {
                const detailEl = document.querySelector('.detail-image');
                if (!detailEl) return;

                const run = () => {
                    if (!currentImage.value || currentImage.value.id !== expectedId) return;
                    const index = currentImageIndex.value;
                    if (index < 0) return;
                    const list = detailImageList.value;
                    // Prefer the next image first because forward navigation is more common.
                    prefetchDetailAsset(list[index + 1]);
                    prefetchDetailAsset(list[index - 1]);
                };

                if (detailEl.complete && detailEl.naturalWidth > 0) {
                    window.setTimeout(run, 0);
                } else {
                    detailEl.addEventListener('load', run, {once: true});
                }
            });
        };

        const showLibraryDetailImmediately = (item, changeView) => {
            const cached = item && item.id ? detailImageInfoCache.get(item.id) : null;
            currentImage.value = cached ? {...item, ...cached} : {...item};
            if (changeView) currentView.value = 'image-detail';
            scheduleAdjacentDetailPrefetch(currentImage.value.id);
        };

        const viewLibraryImage = async (item, changeView = true) => {
            if (!item) return;
            showLibraryDetailImmediately(item, changeView);
            await hydrateCurrentImage(item);
        };

        const viewSharedImage = async (item, changeView = true) => {
            if (!currentShareToken.value || !item) return;
            showLibraryDetailImmediately(item, changeView);
            await hydrateCurrentImage(item);
        };

        const toggleLibraryFavorite = async (image) => {
            try {
                const response = await fetch(`/api/library/sources/${image.source_id}/favorite`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: image.relative_path})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '操作失败');
                    return;
                }
                image.is_favorited = data.is_favorited;
                const sourceItem = (libraryListing.value.items || []).find(i => i.relative_path === image.relative_path);
                if (sourceItem) sourceItem.is_favorited = data.is_favorited;
                const smartItem = smartAlbumImages.value.find(i => i.id === image.id);
                if (smartItem) smartItem.is_favorited = data.is_favorited;
                ElMessage.success(data.is_favorited ? '收藏成功' : '取消收藏');
            } catch (error) {
                ElMessage.error('操作失败');
            }
        };

        const softDeleteCurrentLibraryImage = async () => {
            const image = currentImage.value;
            if (!isAdmin.value || !image || image.source_type !== 'library') return;

            const oldIndex = currentImageIndex.value;
            const filename = image.original_filename || image.name || '当前图片';
            try {
                await ElMessageBox.confirm(
                    `将 “${filename}” 移动到当前 Set 的 Deleted，并保留原目录层级。\n\nDeleted 不会在网页图库中显示，也不能通过网页再次删除；如需永久删除或恢复，请在本地文件系统操作。`,
                    '移动到 Deleted',
                    {
                        confirmButtonText: '移动到 Deleted',
                        cancelButtonText: '取消',
                        type: 'warning'
                    }
                );

                const response = await fetch(`/api/library/sources/${image.source_id}/soft-delete`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: image.relative_path})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '移动失败');

                detailImageInfoCache.delete(image.id);
                if (exifCache.value && Object.prototype.hasOwnProperty.call(exifCache.value, image.id)) {
                    delete exifCache.value[image.id];
                }

                const listingItems = libraryListing.value.items || [];
                const listingIndex = listingItems.findIndex(item => item.relative_path === image.relative_path);
                if (listingIndex !== -1) listingItems.splice(listingIndex, 1);

                if (libraryListing.value.stats) {
                    libraryListing.value.stats.image_count = Math.max(0, (Number(libraryListing.value.stats.image_count) || 0) - 1);
                    libraryListing.value.stats.total_file_count = Math.max(0, (Number(libraryListing.value.stats.total_file_count) || 0) - 1);
                }

                ElMessage.success(`已移动到 ${data.deleted_relative_path || 'Deleted'}`);

                const remaining = detailImageList.value;
                if (!remaining.length) {
                    currentImage.value = {};
                    currentView.value = 'library';
                    return;
                }

                const nextIndex = Math.min(Math.max(oldIndex, 0), remaining.length - 1);
                await viewLibraryImage(remaining[nextIndex], false);
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') {
                    ElMessage.error(error?.message || '移动到 Deleted 失败');
                }
            }
        };

        const toggleCurrentFavorite = async () => {
            if (currentImage.value && currentImage.value.source_type === 'library-share') {
                if (libraryShare.value.allow_select) await toggleShareSelection(currentImage.value);
            } else if (currentImage.value && currentImage.value.source_type === 'library') {
                await toggleLibraryFavorite(currentImage.value);
            } else if (currentImage.value && currentImage.value.id) {
                await toggleFavorite(currentImage.value.id);
            }
        };

        const toggleImageFavorite = async (image) => {
            if (!image) return;
            if (image.source_type === 'library-share') {
                if (libraryShare.value.allow_select) await toggleShareSelection(image);
            } else if (image.source_type === 'library') {
                await toggleLibraryFavorite(image);
            } else {
                await toggleFavorite(image.id);
            }
        };

        const exportLibraryFavorites = () => {
            const names = libraryImages.value.filter(item => item.is_favorited).map(item => item.original_filename);
            if (!names.length) {
                ElMessage.warning('当前目录没有收藏的图片');
                return;
            }
            const text = names.map((name, index) => `${index + 1}. ${name}`).join('\n');

            // 与 uploaded album 的选图交互保持一致：先预览文件名，再由用户确认复制。
            ElMessageBox({
                title: `目录: ${libraryListing.value.name || currentLibrarySource.value?.name || ''}`,
                message: `收藏图片列表 (${names.length}张):\n\n${text}`,
                showConfirmButton: true,
                showCancelButton: true,
                confirmButtonText: '复制到剪贴板',
                cancelButtonText: '关闭',
                customClass: 'favorite-list-box',
                beforeClose: async (action, instance, done) => {
                    if (action === 'confirm') {
                        try {
                            await navigator.clipboard.writeText(text);
                            ElMessage.success('已复制到剪贴板');
                            done();
                        } catch (err) {
                            const textArea = document.createElement('textarea');
                            textArea.value = text;
                            document.body.appendChild(textArea);
                            textArea.select();
                            document.execCommand('copy');
                            document.body.removeChild(textArea);
                            ElMessage.success('已复制到剪贴板');
                            done();
                        }
                    } else {
                        done();
                    }
                }
            });
        };

        const renameLibraryImageFile = async (image, newFilename) => {
            try {
                const response = await fetch(`/api/library/sources/${image.source_id}/rename`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: image.relative_path, new_filename: newFilename})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '重命名失败');
                    return false;
                }
                currentImage.value = data;
                await loadLibraryDirectory(libraryListing.value.path || '');
                currentView.value = 'image-detail';
                ElMessage.success('文件名修改成功');
                return true;
            } catch (error) {
                ElMessage.error('重命名失败');
                return false;
            }
        };

        const updateLibraryDescription = async (image, description) => {
            try {
                const response = await fetch(`/api/library/sources/${image.source_id}/description`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: image.relative_path, description})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '保存失败');
                    return false;
                }
                currentImage.value.description = data.description || '';
                const sourceItem = (libraryListing.value.items || []).find(i => i.relative_path === image.relative_path);
                if (sourceItem) sourceItem.description = data.description || '';
                const smartItem = smartAlbumImages.value.find(i => i.id === image.id);
                if (smartItem) smartItem.description = data.description || '';
                ElMessage.success('描述保存成功');
                return true;
            } catch (error) {
                ElMessage.error('保存失败');
                return false;
            }
        };

        const queryKnownLocations = (query, callback) => manifestAutofill.queryLocations(query, callback);
        const selectKnownLocation = (item) => manifestAutofill.selectLocation(item);
        const syncKnownLocation = (value) => manifestAutofill.syncLocation(value, true);
        const queryKnownSources = (query, callback) => manifestAutofill.querySources(query, callback);
        const selectKnownSource = (item) => manifestAutofill.selectSource(item);
        const syncKnownSource = (value) => manifestAutofill.syncSource(value, true);

        // Historical free-text suggestions. These are editor conveniences only:
        // selecting a suggestion changes the Form exactly like typing the same
        // value would; no hidden JSON fields or cross-field inference is added.
        const queryKnownManifestValue = (kind, query, callback) => manifestAutofill.queryValues(kind, query, callback);
        const queryKnownModels = (query, callback) => queryKnownManifestValue('models', query, callback);
        const queryKnownThemeNames = (query, callback) => queryKnownManifestValue('theme_names', query, callback);
        const queryKnownCharacters = (query, callback) => manifestAutofill.queryCharacters(query, callback);
        const queryKnownVariants = (query, callback) => queryKnownManifestValue('variants', query, callback);
        const queryKnownReferences = (query, callback) => queryKnownManifestValue('references', query, callback);
        const queryKnownOutfits = (query, callback) => queryKnownManifestValue('outfits', query, callback);
        const queryKnownPrimaryPhotographers = (query, callback) => queryKnownManifestValue('primary_photographers', query, callback);
        const queryKnownFixtures = (query, callback) => queryKnownManifestValue('fixtures', query, callback);
        const queryKnownLightingNotes = (query, callback) => queryKnownManifestValue('lighting_notes', query, callback);

        const manifestHistoryOptions = (kind, base = []) => {
            const combined = [
                ...(Array.isArray(base) ? base : []),
                ...(Array.isArray(manifestKnownValues.value?.[kind]) ? manifestKnownValues.value[kind] : [])
            ];
            const seen = new Set();
            return combined.filter(value => {
                const text = String(value ?? '').trim();
                if (!text) return false;
                const key = text.toLocaleLowerCase();
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
            });
        };

        const resetManifestDialogScroll = (dialogClass) => {
            nextTick(() => {
                window.requestAnimationFrame(() => {
                    const dialog = document.querySelector(`.el-dialog.${dialogClass}`)
                        || document.querySelector(`.${dialogClass} .el-dialog`);
                    const body = dialog ? dialog.querySelector('.el-dialog__body') : null;
                    if (body) body.scrollTop = 0;
                });
            });
        };

        const resetManifestDetailScroll = () => resetManifestDialogScroll('manifest-detail-dialog');
        const resetManifestEditorScroll = () => resetManifestDialogScroll('manifest-edit-dialog');

        const openManifestDetails = () => {
            const isShare = currentView.value === 'library-share';
            if ((!isShare && !isSetDirectory.value) || !currentManifestData.value) return;
            showManifestDetailDialog.value = true;
            // Public Set shares reuse the same metadata Detail UI but never run
            // admin-only derived scans or expose edit/workflow actions.
            if (!isShare) {
                window.setTimeout(() => void setInsights.loadDetail(), 0);
            }
        };

        const openManifestEditor = async () => {
            if (!isSetDirectory.value) {
                ElMessage.warning('只有 Set 目录可以创建或编辑 manifest');
                return;
            }
            const manifest = libraryListing.value.manifest || {};
            if (manifest.exists && !manifest.valid) {
                ElMessage.error('manifest.json 当前无法解析，请先修复 JSON 格式');
                return;
            }
            const data = manifest.valid ? manifest.data : (libraryListing.value.suggested_manifest || {});
            manifestEditPath.value = libraryListing.value.path || '';
            manifestForm.value = normalizeManifest(data);
            manifestEditorMode.value = 'form';
            // Raw JSON must reflect the manifest file itself. Using the parsed object
            // here lets the server JSON serializer reorder top-level keys, which is
            // especially dangerous for hand-maintained manifests. Existing files use
            // the original text returned by _read_manifest; new manifests fall back
            // to the suggested object.
            manifestJsonText.value = manifest.valid && typeof manifest.raw === 'string'
                ? manifest.raw
                : JSON.stringify(orderManifestKeys(data), null, 2);
            manifestJsonError.value = '';
            showManifestDialog.value = true;

            // EXIF scanning can be slow on a large Original/JPG folder. Do it only
            // after the editor is opened, never as part of ordinary Set browsing.
            if (!manifest.valid) {
                try {
                    const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/manifest-suggestion?path=${encodeURIComponent(manifestEditPath.value || '')}`);
                    const suggested = await response.json();
                    if (!response.ok) throw new Error(suggested.error || '读取拍摄时间失败');
                    if (!manifestForm.value.shoot.start_time && suggested.shoot?.start_time) {
                        manifestForm.value.shoot.start_time = suggested.shoot.start_time;
                    }
                    if (!manifestForm.value.shoot.end_time && suggested.shoot?.end_time) {
                        manifestForm.value.shoot.end_time = suggested.shoot.end_time;
                    }
                } catch (error) {
                    ElMessage.warning(error?.message || '读取 Original/JPG EXIF 时间失败');
                }
            }

            await manifestAutofill.syncCurrent();
            manifestKnownValues.value = manifestAutofill.getValues();
        };

        const editManifestFromDetail = () => {
            showManifestDetailDialog.value = false;
            openManifestEditor();
        };

        const saveManifest = async () => {
            let payload;
            if (manifestEditorMode.value === 'json') {
                payload = parseManifestJsonText();
                if (!payload) {
                    ElMessage.error('JSON 格式有误，无法保存');
                    return;
                }
                // Raw JSON keeps values/custom fields pass-through; only common
                // storage cleanup and known-field ordering are applied.
                payload = applyManifestCommonStorageRules(payload);
            } else {
                const formPayload = applyManifestStorageRules(trimManifestFormStrings(serializeManifestForm()));
                const currentRaw = parseManifestJsonText();
                if (!currentRaw) {
                    ElMessage.error('当前 JSON 格式有误，不能安全保留自定义字段');
                    return;
                }
                // Merge Form-owned fields back into the raw manifest so custom
                // fields (e.g. project/workflow) survive Form saves unchanged.
                payload = applyManifestStorageRules(mergeManifestFormIntoRaw(currentRaw, formPayload));
            }
            try {
                const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/manifest?path=${encodeURIComponent(manifestEditPath.value || '')}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '保存失败');
                    return;
                }
                showManifestDialog.value = false;
                manifestAutofill.invalidate();
                ElMessage.success('manifest.json 已保存');
                await loadLibraryDirectory(libraryListing.value.path || '');
            } catch (error) {
                ElMessage.error('保存 manifest 失败');
            }
        };

        const openManifestArray = async () => {
            if (!isLibraryRoot.value || !currentLibrarySource.value) {
                ElMessage.warning('Manifest Array 只能在 Set 父目录使用');
                return;
            }
            manifestArrayVisible.value = true;
            manifestArrayLoading.value = true;
            manifestArrayText.value = '';
            manifestArrayCount.value = 0;
            try {
                const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/manifests`);
                const data = await response.json();
                if (!response.ok) {
                    const details = Array.isArray(data.errors)
                        ? data.errors.map(item => `${item.path}: ${item.error}`).join('\n')
                        : '';
                    throw new Error(details ? `${data.error || '读取 manifest 失败'}\n${details}` : (data.error || '读取 manifest 失败'));
                }
                manifestArrayCount.value = Number(data.count) || 0;
                manifestArrayText.value = JSON.stringify(Array.isArray(data.manifests) ? data.manifests : [], null, 2);
            } catch (error) {
                manifestArrayText.value = '';
                ElMessage.error(error?.message || '读取 manifest 失败');
            } finally {
                manifestArrayLoading.value = false;
            }
        };

        const openFolderCompareTool = () => {
            if (!isLibraryRoot.value) {
                ElMessage.warning('Compare Folders 只能在 Set 父目录使用');
                return;
            }
            window.open('/folder-compare.html', '_blank', 'noopener');
        };

        const removeLibraryDotfiles = async () => {
            if (!isLibraryRoot.value || !currentLibrarySource.value) {
                ElMessage.warning('Remove Dotfiles 只能在 Set 父目录使用');
                return;
            }

            try {
                await ElMessageBox.confirm(
                    '将递归删除当前 Set 父目录中的 .DS_Store 和 ._* 文件。其他隐藏文件不会删除。此操作不可撤销，是否继续？',
                    'Remove Dotfiles',
                    {
                        type: 'warning',
                        confirmButtonText: '删除',
                        cancelButtonText: '取消'
                    }
                );
            } catch (error) {
                return;
            }

            try {
                const response = await fetch(`/api/library/sources/${currentLibrarySource.value.id}/remove-dotfiles`, {
                    method: 'POST'
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Remove Dotfiles 失败');

                const removed = Number(data.removed_count) || 0;
                const failed = Number(data.failed_count) || 0;
                if (failed > 0) {
                    ElMessage.warning(`已删除 ${removed} 个 dotfiles；${failed} 个删除失败`);
                } else if (removed > 0) {
                    ElMessage.success(`已删除 ${removed} 个 dotfiles`);
                } else {
                    ElMessage.info('没有发现 .DS_Store 或 ._* 文件');
                }
            } catch (error) {
                ElMessage.error(error?.message || 'Remove Dotfiles 失败');
            }
        };

        const copyManifestArray = async () => {
            if (!manifestArrayText.value) return;
            try {
                await navigator.clipboard.writeText(manifestArrayText.value);
                ElMessage.success(`已复制 ${manifestArrayCount.value} 条 manifest`);
            } catch (error) {
                ElMessage.error('复制失败，请在文本框中手动复制');
            }
        };

        const openLibraryShareDialog = () => {
            if (!isSetDirectory.value) {
                ElMessage.warning('只能分享完整 Set');
                return;
            }
            libraryShareForm.value = {
                title: libraryListing.value.name || '选片',
                password: '',
                allow_select: true
            };
            showLibraryShareDialog.value = true;
        };

        const createLibraryShare = async () => {
            try {
                const response = await fetch('/api/library/shares', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        source_id: currentLibrarySource.value.id,
                        relative_path: libraryListing.value.path || '',
                        title: libraryShareForm.value.title,
                        password: libraryShareForm.value.password,
                        allow_select: libraryShareForm.value.allow_select
                    })
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '创建分享失败');
                    return;
                }
                showLibraryShareDialog.value = false;
                const shareUrl = `${window.location.origin}${window.location.pathname}?share=${data.token}`;
                await ElMessageBox.confirm(shareUrl, '分享链接已创建', {
                    confirmButtonText: '复制链接', cancelButtonText: '关闭', type: 'success'
                }).then(async () => {
                    await navigator.clipboard.writeText(shareUrl);
                    ElMessage.success('链接已复制');
                }).catch(() => {});
            } catch (error) {
                ElMessage.error('创建分享失败');
            }
        };

        const applyLibrarySharePayload = (data) => {
            shareNeedsPassword.value = false;
            libraryShare.value = data;
            currentView.value = 'library-share';
        };

        const loadLibraryShare = async (token) => {
            currentShareToken.value = token;
            shareLoading.value = true;
            currentView.value = 'library-share';
            try {
                const response = await fetch(`/api/library/shares/${encodeURIComponent(token)}`);
                const data = await response.json();
                if (response.status === 401 && data.needs_password) {
                    shareNeedsPassword.value = true;
                    libraryShare.value = {title: data.title || '分享 Set', listing: {items: []}};
                    return;
                }
                if (!response.ok) {
                    ElMessage.error(data.error || '分享链接不可用');
                    libraryShare.value = {title: '分享不可用', listing: {items: []}};
                    return;
                }
                applyLibrarySharePayload(data);
            } catch (error) {
                ElMessage.error('加载分享失败');
                libraryShare.value = {title: '分享不可用', listing: {items: []}};
            } finally {
                shareLoading.value = false;
            }
        };

        const loadLibraryShareDirectory = async (path) => {
            if (!currentShareToken.value) return;
            shareLoading.value = true;
            try {
                const response = await fetch(`/api/library/shares/${encodeURIComponent(currentShareToken.value)}/browse?path=${encodeURIComponent(path || '')}`);
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '目录读取失败');
                    return;
                }
                applyLibrarySharePayload(data);
                await scrollLibraryPageTop();
            } catch (error) {
                ElMessage.error('目录读取失败');
            } finally {
                shareLoading.value = false;
            }
        };

        const openLibraryShareDirectory = async (item) => {
            if (!item || item.type !== 'directory') return;
            await loadLibraryShareDirectory(item.relative_path);
        };

        const libraryShareBack = async () => {
            const parent = libraryShare.value.listing?.parent_path;
            if (parent === null || parent === undefined) return;
            await loadLibraryShareDirectory(parent);
        };


        const unlockLibraryShare = async () => {
            try {
                const response = await fetch(`/api/library/shares/${encodeURIComponent(currentShareToken.value)}/unlock`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: sharePassword.value})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '密码错误');
                    return;
                }
                sharePassword.value = '';
                await loadLibraryShare(currentShareToken.value);
            } catch (error) {
                ElMessage.error('验证失败');
            }
        };

        const sharePathAssetUrl = (relativePath, variant = 'thumbnail') => {
            if (!relativePath) return '';
            return `/api/library/shares/${encodeURIComponent(currentShareToken.value)}/asset?variant=${encodeURIComponent(variant)}&path=${encodeURIComponent(relativePath)}`;
        };

        const shareAssetUrl = (item, variant = 'thumbnail') => {
            if (!item) return '';
            return sharePathAssetUrl(item.relative_path, variant);
        };

        const toggleShareSelection = async (item) => {
            if (!libraryShare.value.allow_select) return;
            try {
                const response = await fetch(`/api/library/shares/${encodeURIComponent(currentShareToken.value)}/selection`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: item.relative_path})
                });
                const data = await response.json();
                if (!response.ok) {
                    ElMessage.error(data.error || '选片失败');
                    return;
                }
                const raw = (libraryShare.value.listing?.items || []).find(img => img.relative_path === item.relative_path);
                if (raw) {
                    raw.selected = data.selected;
                    raw.is_favorited = data.selected;
                }
                if (currentImage.value && currentImage.value.source_type === 'library-share' && currentImage.value.relative_path === item.relative_path) {
                    currentImage.value.is_favorited = data.selected;
                }
                ElMessage.success(data.selected ? '收藏成功' : '取消收藏');
            } catch (error) {
                ElMessage.error('选片失败');
            }
        };

        const exportShareSelection = async () => {
            const names = shareSelectedImages.value.map(item => item.original_filename);
            if (!names.length) {
                ElMessage.warning('还没有选择图片');
                return;
            }
            const text = names.map((name, index) => `${index + 1}. ${name}`).join('\n');
            try {
                await navigator.clipboard.writeText(text);
                ElMessage.success(`已复制 ${names.length} 个文件名`);
            } catch (error) {
                ElMessageBox.alert(text, '已选文件名', {confirmButtonText: '关闭'});
            }
        };

        const manifestSummary = computed(() => {
            if (!currentManifestData.value) return null;
            const isShareRoot = currentView.value === 'library-share' && !!libraryShare.value.listing?.is_share_root;
            if (!isShareRoot && !isSetDirectory.value) return null;
            const data = currentManifestData.value;
            const fallbackName = isShareRoot ? (libraryShare.value.title || '') : (libraryListing.value.name || '');
            return {
                title: data.theme.name || fallbackName,
                date: data.shoot.date || '',
                model: data.model || '',
                location: data.location.name || '',
                theme: data.theme.name || ''
            };
        });


        // 添加URL工具函数
        const updateUrlWithoutParams = () => {
            // 去掉所有查询参数，只保留路径
            const newUrl = window.location.pathname;
            if (window.location.search) {
                window.history.replaceState({}, '', newUrl);
            }
        };

        onMounted(async () => {
            const urlParams = new URLSearchParams(window.location.search);
            const shareToken = urlParams.get('share');
            const sourceId = urlParams.get('source');
            const sourcePath = urlParams.get('path') || '';
            const albumId = urlParams.get('album');
            const imageId = urlParams.get('image');

            loadSiteTitle();
            document.addEventListener('keydown', handleKeyDown);

            // 公共分享页不需要管理员状态，也不加载传统相册列表。
            if (shareToken) {
                await loadLibraryShare(shareToken);
                return;
            }

            await loadAlbums();
            await loadSiteConfig();
            await restoreAlbumAccessTokens();
            await restoreAdminStatus();
            loadCollapsedGroups();

            if (isAdmin.value) {
                await loadLibrarySources();
                await loadSmartViews();
            }

            if (sourceId && isAdmin.value) {
                const source = librarySources.value.find(s => String(s.id) === String(sourceId));
                if (source) {
                    await openLibrarySource(source, sourcePath);
                    return;
                }
            }

            if (albumId) {
                if (imageId) {
                    setTimeout(async () => {
                        await openAlbumDirect(parseInt(albumId), parseInt(imageId));
                    }, 100);
                    return;
                }
                setTimeout(async () => {
                    await openAlbumDirect(parseInt(albumId));
                }, 100);
            }
        });


        // 重命名图片文件
        const renameImageFile = async (imageId, newFilename) => {
            try {
                const response = await fetch(`/api/images/${imageId}/rename`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        new_filename: newFilename
                    })
                });

                if (response.ok) {
                    // 更新本地数据
                    const imageIndex = images.value.findIndex(img => img.id === imageId);
                    if (imageIndex !== -1) {
                        images.value[imageIndex].original_filename = newFilename;
                    }

                    // 如果当前正在查看的图片被重命名，也更新当前图片状态
                    if (currentImage.value && currentImage.value.id === imageId) {
                        currentImage.value.original_filename = newFilename;
                    }

                    ElMessage.success('文件名修改成功');
                } else {
                    const data = await response.json();
                    ElMessage.error(data.error || '重命名失败');
                }
            } catch (error) {
                ElMessage.error('重命名失败');
            }
        };

        // 批量删除图片
        const batchDeleteImages = async () => {
            if (selectedImages.value.length === 0) return;

            try {
                await ElMessageBox.confirm(
                    `确定要删除选中的 ${selectedImages.value.length} 张图片吗？`,
                    '警告',
                    {
                        confirmButtonText: '确定',
                        cancelButtonText: '取消',
                        type: 'warning',
                    }
                );

                // 逐个删除选中的图片
                const deletePromises = selectedImages.value.map(imageId =>
                    fetch(`/api/images/${imageId}`, {method: 'DELETE'})
                );

                await Promise.all(deletePromises);

                ElMessage.success(`成功删除 ${selectedImages.value.length} 张图片`);

                // 清除选择状态并重新加载图片
                selectedImages.value = [];
                selectionMode.value = false; // 退出选择模式
                await loadAlbumImages(currentAlbum.value.id);

                // 重新加载相册列表（更新图片数量）
                await loadAlbums();


                // // 如果删除了封面图片，重新加载相册列表
                // if (selectedImages.value.includes(currentAlbum.value.cover_image_id)) {
                //     loadAlbums();
                // }

            } catch (error) {
                if (error !== 'cancel') {
                    ElMessage.error('批量删除失败');
                }
            }
        };

        // fav start
        const toggleFavorite = async (imageId) => {
            try {
                const response = await fetch(`/api/images/${imageId}/favorite`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    }
                });

                if (response.ok) {
                    const data = await response.json();
                    // 更新本地状态
                    const imageIndex = images.value.findIndex(img => img.id === imageId);
                    if (imageIndex !== -1) {
                        images.value[imageIndex].is_favorited = data.is_favorited;
                    }
                    // 如果当前正在查看的图片被收藏/取消收藏，也更新当前图片状态
                    if (currentImage.value && currentImage.value.id === imageId) {
                        currentImage.value.is_favorited = data.is_favorited;
                    }

                    ElMessage.success(data.is_favorited ? '收藏成功' : '取消收藏');
                }
            } catch (error) {
                ElMessage.error('操作失败');
            }
        };
        // fav end


        // select start

        // 添加多选状态
        const selectionMode = ref(false);
        const selectedImages = ref([]);


        // 切换选择模式
        const toggleSelectionMode = () => {
            selectionMode.value = !selectionMode.value;
            if (!selectionMode.value) {
                // 退出选择模式时清空选择
                selectedImages.value = [];
            }
        };


        const handleImageClick = (imageId) => {
            if (selectionMode.value) {
                // 选择模式下切换选择状态
                const index = selectedImages.value.indexOf(imageId);
                if (index > -1) {
                    selectedImages.value.splice(index, 1);
                } else {
                    selectedImages.value.push(imageId);
                }
            } else {
                // 正常模式下查看图片
                viewImage(imageId);
            }
        };


        const isAllSelected = computed(() => {
            return selectionMode.value &&
                filteredImages.value.length > 0 &&
                selectedImages.value.length === filteredImages.value.length;
        });


        const selectAllImages = () => {
            if (selectedImages.value.length === filteredImages.value.length) {
                // 如果已经全选，则清空选择
                selectedImages.value = [];
            } else {
                // 否则选择所有筛选后的图片
                selectedImages.value = filteredImages.value.map(img => img.id);
            }
        };

        // select end

        // password start

        // 添加密码管理状态
        const passwordEnabled = ref(false);
        const newPassword = ref('');
        const albumAccessTokens = ref({});

        // 处理密码开关
        const handlePasswordToggle = (enabled) => {
            if (!enabled) {
                // 关闭密码保护
                removeAlbumPassword();
            }
        };

        // 设置相册密码
        const setAlbumPassword = async () => {
            if (!newPassword.value.trim()) {
                ElMessage.warning('请输入密码');
                return;
            }

            try {
                const response = await fetch(`/api/albums/${currentAlbum.value.id}/password`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        password: newPassword.value
                    })
                });

                if (response.ok) {
                    ElMessage.success('密码设置成功');
                    newPassword.value = '';
                } else {
                    ElMessage.error('密码设置失败');
                }
            } catch (error) {
                ElMessage.error('密码设置失败');
            }
        };

        // 移除相册密码
        const removeAlbumPassword = async () => {
            try {
                const response = await fetch(`/api/albums/${currentAlbum.value.id}/password`, {
                    method: 'DELETE'
                });

                if (response.ok) {
                    ElMessage.success('密码已移除');
                    passwordEnabled.value = false;
                } else {
                    ElMessage.error('移除密码失败');
                }
            } catch (error) {
                ElMessage.error('移除密码失败');
            }
        };


        watch(showEditAlbumDialog, (newVal) => {
            if (newVal && currentAlbum.value) {
                // 检查相册是否有密码
                checkAlbumPasswordStatus();
            }
        });

        const checkAlbumPasswordStatus = async () => {
            try {
                const response = await fetch(`/api/albums/${currentAlbum.value.id}/has-password`);
                const data = await response.json();
                passwordEnabled.value = data.has_password;
                newPassword.value = '';
            } catch (error) {
                console.error('检查密码状态失败:', error);
            }
        };


        // 显示密码输入对话框
        const showPasswordDialog = (album) => {
            return new Promise((resolve) => {
                ElMessageBox.prompt('此相册已加密，请输入访问密码', '密码验证', {
                    confirmButtonText: '确定',
                    cancelButtonText: '取消',
                    inputType: 'password',

                    inputPlaceholder: '请输入密码',
                    beforeClose: async (action, instance, done) => {
                        if (action === 'confirm') {
                            const password = instance.inputValue;
                            try {
                                const response = await fetch(`/api/albums/${album.id}/verify-password`, {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json'
                                    },
                                    body: JSON.stringify({password})
                                });

                                if (response.ok) {
                                    const data = await response.json();

                                    // 存储token到内存和localStorage
                                    albumAccessTokens.value[album.id] = data.token;

                                    // 存储token到localStorage以便刷新后恢复
                                    localStorage.setItem(`album_${album.id}_token`, data.token);

                                    ElMessage.success('密码验证成功');
                                    done();
                                    resolve(true);
                                } else {
                                    const data = await response.json();
                                    ElMessage.error(data.error || '密码错误');
                                    instance.inputValue = '';
                                }
                            } catch (error) {
                                ElMessage.error('验证失败');
                            }
                        } else {
                            done();
                            resolve(false);
                        }
                    }
                });
            });
        };


        // 检查相册访问权限
        const checkAlbumAccess = (albumId) => {
            // 只要有token就认为可以访问，具体验证交给后端
            return !!albumAccessTokens.value[albumId];
        };

        const restoreAlbumAccessTokens = async () => {
            const verifiedTokens = {};

            // 先收集所有需要验证的键
            const tokenKeys = [];
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key && key.startsWith('album_') && key.endsWith('_token')) {
                    tokenKeys.push(key);
                }
            }

            // 然后验证每个token
            for (const key of tokenKeys) {
                const albumId = key.replace('album_', '').replace('_token', '');
                const token = localStorage.getItem(key);

                if (token) {
                    try {
                        const response = await fetch(`/api/albums/${albumId}/verify-token`, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({token})
                        });

                        if (response.ok) {
                            const data = await response.json();
                            if (data.valid) {
                                verifiedTokens[albumId] = token;
                            } else {
                                // token无效，清除
                                localStorage.removeItem(`album_${albumId}_token`);
                            }
                        }
                    } catch (error) {
                        console.error('验证token失败:', error);
                    }
                }
            }

            // 恢复有效的token
            for (const [albumId, token] of Object.entries(verifiedTokens)) {
                albumAccessTokens.value[albumId] = token;
            }
        };


        // password end

        // desc start

        const editImageFilename = async (image) => {
            const fullFilename = image.original_filename || '';
            const lastDotIndex = fullFilename.lastIndexOf('.');

            // 使用 Element Plus 的 Form 对话框
            try {
                const result = await ElMessageBox({
                    title: '重命名文件',
                    message: `
                <div id="rename-form" style="padding: 10px 0;">
                    <p style="margin-bottom: 15px; color: #666;">当前: <strong>${fullFilename}</strong></p>
                    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                        <div style="flex: 1;">
                            <label style="display: block; margin-bottom: 5px; color: #666;">文件名:</label>
                            <input id="name-input" type="text" class="rename-input" 
                                   value="${lastDotIndex > 0 ? fullFilename.substring(0, lastDotIndex) : fullFilename}">
                        </div>
                        <div style="width: 80px;">
                            <label style="display: block; margin-bottom: 5px; color: #666;">扩展名:</label>
                            <input id="ext-input" type="text" class="rename-input" 
                                   value="${lastDotIndex > 0 ? fullFilename.substring(lastDotIndex + 1) : ''}">
                        </div>
                    </div>
                </div>
                <style>
                    .rename-input {
                        width: 100%;
                        padding: 8px 12px;
                        border: 1px solid #dcdfe6;
                        border-radius: 4px;
                        font-size: 14px;
                    }
                    .rename-input:focus {
                        border-color: #409eff;
                        outline: none;
                    }
                </style>
            `,
                    showConfirmButton: true,
                    showCancelButton: true,
                    confirmButtonText: '保存',
                    cancelButtonText: '取消',
                    dangerouslyUseHTMLString: true,
                    beforeClose: async (action, instance, done) => {
                        if (action === 'confirm') {
                            const nameInput = document.getElementById('name-input');
                            const extInput = document.getElementById('ext-input');

                            const name = nameInput ? nameInput.value.trim() : '';
                            const ext = extInput ? extInput.value.trim() : '';

                            if (!name) {
                                ElMessage.warning('文件名不能为空');
                                return;
                            }

                            const newFilename = ext ? `${name}.${ext}` : name;

                            if (newFilename === fullFilename) {
                                done();
                                return;
                            }

                            if (image.source_type === 'library') {
                                await renameLibraryImageFile(image, newFilename);
                            } else {
                                await renameImageFile(image.id, newFilename);
                            }
                            done();
                        } else {
                            done();
                        }
                    }
                });
            } catch (error) {
                if (error !== 'cancel') {
                    ElMessage.error('操作失败');
                }
            }
        };

        const editImageDescription = async (image) => {
            try {
                const {value} = await ElMessageBox.prompt('请输入图片描述', '编辑描述', {
                    confirmButtonText: '保存',
                    cancelButtonText: '取消',
                    inputValue: image.description || '',
                    inputPlaceholder: '请输入图片描述...',
                    inputType: 'textarea',
                });

                if (value !== null) {
                    if (image.source_type === 'library') {
                        await updateLibraryDescription(image, value);
                        return;
                    }
                    const response = await fetch(`/api/images/${image.id}/description`, {
                        method: 'PUT',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            description: value
                        })
                    });

                    if (response.ok) {
                        // 更新本地数据
                        const imageIndex = images.value.findIndex(img => img.id === image.id);
                        if (imageIndex !== -1) {
                            images.value[imageIndex].description = value;
                        }

                        // 如果当前正在查看的图片被编辑，也更新当前图片状态
                        if (currentImage.value && currentImage.value.id === image.id) {
                            currentImage.value.description = value;
                        }

                        ElMessage.success('描述保存成功');
                    } else {
                        ElMessage.error('保存失败');
                    }
                }
            } catch (error) {
                if (error !== 'cancel') {
                    ElMessage.error('操作失败');
                }
            }
        };
        // desc end


        // exif start
        const showExifDialog = ref(false);
        const exifData = ref(null);
        const currentExifImageId = ref(null);

        // EXIF表格数据
        const exifTableData = computed(() => {
            if (!exifData.value) return [];

            const tableData = [];
            const flattenObject = (obj, prefix = '') => {
                for (const key in obj) {
                    if (obj.hasOwnProperty(key)) {
                        const fullKey = prefix ? `${prefix}.${key}` : key;
                        const value = obj[key];

                        if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
                            flattenObject(value, fullKey);
                        } else {
                            tableData.push({
                                key: fullKey,
                                value: Array.isArray(value) ? JSON.stringify(value) : String(value)
                            });
                        }
                    }
                }
            };

            flattenObject(exifData.value);
            return tableData.sort((a, b) => a.key.localeCompare(b.key));
        });

        // 显示图片EXIF信息
        const showImageExif = async (imageId) => {
            showExifDialog.value = true;
            currentExifImageId.value = imageId;
            exifData.value = null;

            try {
                let response;
                if (currentImage.value && currentImage.value.source_type === 'library') {
                    response = await fetch(`/api/library/sources/${currentImage.value.source_id}/exif?path=${encodeURIComponent(currentImage.value.relative_path)}`);
                } else if (currentImage.value && currentImage.value.source_type === 'library-share') {
                    response = await fetch(`/api/library/shares/${encodeURIComponent(currentImage.value.share_token || currentShareToken.value)}/exif?path=${encodeURIComponent(currentImage.value.relative_path)}`);
                } else {
                    response = await fetch(`/api/images/${imageId}/exif`);
                }
                if (response.ok) {
                    const data = await response.json();
                    exifData.value = data.exif || {};
                } else {
                    ElMessage.error('获取EXIF信息失败');
                }
            } catch (error) {
                ElMessage.error('获取EXIF信息失败');
            }
        };
        // exif end


        // title start
        const siteTitle = ref('我的相册');

        // 加载站点标题
        const loadSiteTitle = async () => {
            try {
                const response = await fetch('/api/albums/title');
                const data = await response.json();
                siteTitle.value = data.title || '我的相册';
                // 更新网页标题
                document.title = siteTitle.value;
            } catch (error) {
                console.error('加载标题失败:', error);
            }
        };

        // 编辑站点标题
        const editSiteTitle = async () => {
            try {
                const {value} = await ElMessageBox.prompt('请输入新的标题', '修改标题', {
                    confirmButtonText: '保存',
                    cancelButtonText: '取消',
                    inputValue: siteTitle.value,
                    inputPlaceholder: '请输入标题...',
                    inputValidator: (value) => {
                        if (!value || value.trim() === '') {
                            return '标题不能为空';
                        }
                        if (value.length > 50) {
                            return '标题不能超过50个字符';
                        }
                        return true;
                    }
                });

                if (value !== null && value.trim() !== '' && value !== siteTitle.value) {
                    await saveSiteTitle(value.trim());
                }
            } catch (error) {
                if (error !== 'cancel') {
                    ElMessage.error('操作失败');
                }
            }
        };

        // 保存站点标题
        const saveSiteTitle = async (newTitle) => {
            try {
                const response = await fetch('/api/albums/title', {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        title: newTitle
                    })
                });

                if (response.ok) {
                    const data = await response.json();
                    siteTitle.value = data.title;
                    // 更新网页标题
                    document.title = siteTitle.value;
                    ElMessage.success('标题更新成功');
                } else {
                    const errorData = await response.json();
                    ElMessage.error(errorData.error || '更新失败');
                }
            } catch (error) {
                ElMessage.error('更新标题失败');
            }
        };
        //title end

        // move start
        const showMoveToAlbumDialog = ref(false);
        const targetAlbumId = ref(null);
        const otherAlbums = ref([]);

        // 显示移动对话框
        const showMoveDialog = async () => {
            if (selectedImages.value.length === 0) return;

            try {
                // 获取其他相册列表（排除当前相册）
                const response = await fetch('/api/albums');
                const allAlbums = await response.json();

                otherAlbums.value = allAlbums.filter(album => album.id !== currentAlbum.value.id);

                if (otherAlbums.value.length === 0) {
                    ElMessage.warning('没有其他相册可以移动');
                    return;
                }

                targetAlbumId.value = null;
                showMoveToAlbumDialog.value = true;
            } catch (error) {
                ElMessage.error('加载相册列表失败');
            }
        };

        // 移动选中的图片
        const moveSelectedImages = async () => {
            if (!targetAlbumId.value || selectedImages.value.length === 0) return;

            if (targetAlbumId.value === currentAlbum.value.id) {
                ElMessage.warning('不能移动到当前相册');
                return;
            }

            try {
                const response = await fetch('/api/images/move', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        image_ids: selectedImages.value,
                        target_album_id: targetAlbumId.value
                    })
                });

                if (response.ok) {
                    const data = await response.json();

                    // 清空选择并重新加载图片
                    selectedImages.value = [];
                    selectionMode.value = false;

                    await loadAlbumImages(currentAlbum.value.id);

                    // 重新加载相册列表（更新两个相册的图片数量）
                    await loadAlbums();

                    ElMessage.success(data.message);
                    showMoveToAlbumDialog.value = false;

                    // 重新加载相册列表以更新图片数量
                    loadAlbums();
                } else {
                    const errorData = await response.json();
                    ElMessage.error(errorData.error || '移动失败');
                }
            } catch (error) {
                ElMessage.error('移动图片失败');
            }
        };
        // move end

        //sort start

        // 前端排序函数
        const sortImages = () => {
            if (!images.value.length) return;

            images.value.sort((a, b) => {
                let valueA, valueB;

                switch (currentSort.value.field) {
                    case 'original_filename':
                        valueA = a.original_filename.toLowerCase();
                        valueB = b.original_filename.toLowerCase();
                        break;
                    case 'file_size':
                        valueA = a.file_size || 0;
                        valueB = b.file_size || 0;
                        break;
                    case 'uploaded_at':
                    default:
                        valueA = new Date(a.uploaded_at).getTime();
                        valueB = new Date(b.uploaded_at).getTime();
                        break;
                }

                // 处理null或undefined值
                if (valueA == null) valueA = '';
                if (valueB == null) valueB = '';

                let result = 0;
                if (valueA < valueB) result = -1;
                if (valueA > valueB) result = 1;

                // 根据排序顺序调整
                return currentSort.value.order === 'desc' ? -result : result;
            });
        };


        const currentSort = ref({field: 'original_filename', order: 'asc'});


        // 排序选项
        const sortOptions = [
            {label: '文件名 (A-Z)', value: {field: 'original_filename', order: 'asc'}},
            {label: '文件名 (Z-A)', value: {field: 'original_filename', order: 'desc'}},
            {label: '上传时间 (最新)', value: {field: 'uploaded_at', order: 'desc'}},
            {label: '上传时间 (最旧)', value: {field: 'uploaded_at', order: 'asc'}},

        ];

        // 改变排序方式
        const changeSort = (option) => {
            currentSort.value = {...option.value};
            sortImages();
        };

        // 获取当前排序标签
        const getCurrentSortLabel = computed(() => {
            const option = sortOptions.find(opt =>
                opt.value.field === currentSort.value.field &&
                opt.value.order === currentSort.value.order
            );
            return option ? option.label : '排序方式';
        });

        const changeSmartAlbumSort = (option) => {
            smartAlbumSort.value = {...option.value};
        };

        const getCurrentSmartAlbumSortLabel = computed(() => {
            const option = smartAlbumSortOptions.find(opt =>
                opt.value.field === smartAlbumSort.value.field &&
                opt.value.order === smartAlbumSort.value.order
            );
            return option ? option.label : '查询顺序';
        });

        // sort end


        //export fav start

        // 计算收藏图片数量
        const getFavoriteCount = computed(() => {
            return images.value.filter(img => img.is_favorited).length;
        });


        const exportFavoriteList = () => {
            // 获取收藏的图片
            const favoriteImages = images.value.filter(img => img.is_favorited);

            if (favoriteImages.length === 0) {
                ElMessage.warning('当前相册没有收藏的图片');
                return;
            }

            // 构建文件名列表
            const fileNames = favoriteImages.map(img => img.original_filename);
            const fileListText = fileNames.map((name, index) => `${index + 1}. ${name}`).join('\n');

            // 使用 ElMessageBox 显示对话框，带自定义按钮
            ElMessageBox({
                title: `相册: ${currentAlbum.value.name}`,
                message: `收藏图片列表 (${favoriteImages.length}张):\n\n${fileListText}`,
                showConfirmButton: true,
                showCancelButton: true,
                confirmButtonText: '复制到剪贴板',
                cancelButtonText: '关闭',
                customClass: 'favorite-list-box',
                beforeClose: async (action, instance, done) => {
                    if (action === 'confirm') {
                        // 点击复制按钮
                        try {
                            await navigator.clipboard.writeText(fileListText);
                            ElMessage.success('已复制到剪贴板');
                            done();
                        } catch (err) {
                            // 降级方案：使用老式复制方法
                            const textArea = document.createElement('textarea');
                            textArea.value = fileListText;
                            document.body.appendChild(textArea);
                            textArea.select();
                            document.execCommand('copy');
                            document.body.removeChild(textArea);
                            ElMessage.success('已复制到剪贴板');
                            done();
                        }
                    } else {
                        // 点击关闭按钮
                        done();
                    }
                }
            });
        };

        //export fav end

        //group start
        const albumGroups = ref([]);
        const ungroupedAlbums = ref([]);
        const allGroups = ref([]);
        const showManageGroupsDialog = ref(false);
        const showEditGroupDialog = ref(false);
        const newGroupName = ref('');
        const groupForm = ref({
            id: null,
            name: '',
            sort_order: 0
        });
        const editingGroup = ref(null);

        const createGroup = async () => {
            if (!newGroupName.value.trim()) {
                ElMessage.warning('请输入分组名称');
                return;
            }

            try {
                const response = await fetch('/api/album-groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        name: newGroupName.value.trim(),
                        sort_order: 0
                    })
                });

                if (response.ok) {
                    ElMessage.success('分组创建成功');
                    newGroupName.value = '';
                    loadAlbums();
                } else {
                    const data = await response.json();
                    ElMessage.error(data.error || '创建分组失败');
                }
            } catch (error) {
                ElMessage.error('创建分组失败');
            }
        };

        const editGroup = (group) => {
            editingGroup.value = group;
            groupForm.value = {
                id: group.id,
                name: group.name,
                sort_order: group.sort_order || 0
            };
            showEditGroupDialog.value = true;
        };

        const saveGroup = async () => {
            if (!groupForm.value.name.trim()) {
                ElMessage.warning('请输入分组名称');
                return;
            }

            try {
                const url = editingGroup.value
                    ? `/api/album-groups/${editingGroup.value.id}`
                    : '/api/album-groups';

                const response = await fetch(url, {
                    method: editingGroup.value ? 'PUT' : 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(groupForm.value)
                });

                if (response.ok) {
                    ElMessage.success(editingGroup.value ? '分组更新成功' : '分组创建成功');
                    showEditGroupDialog.value = false;
                    editingGroup.value = null;
                    groupForm.value = {id: null, name: '', sort_order: 0};
                    loadAlbums();
                } else {
                    const data = await response.json();
                    ElMessage.error(data.error || '操作失败');
                }
            } catch (error) {
                ElMessage.error('操作失败');
            }
        };

        const deleteGroup = async (group) => {
            try {
                // 询问用户如何处理分组中的相册
                const confirmResult = await ElMessageBox.confirm(
                    `确定要删除分组 "${group.name}" 吗？\n分组中的 ${group.album_count} 个相册将变为未分组状态。`,
                    '删除分组',
                    {
                        confirmButtonText: '删除并移到未分组',
                        cancelButtonText: '取消',
                        type: 'warning',
                        distinguishCancelAndClose: true
                    }
                );

                const response = await fetch(`/api/album-groups/${group.id}`, {
                    method: 'DELETE',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        move_to_ungrouped: true
                    })
                });

                if (response.ok) {
                    const data = await response.json();
                    ElMessage.success(data.message);

                    // 重新加载相册列表
                    await loadAlbums();

                    // 如果有受影响的相册，显示提示
                    if (data.affected_albums && data.affected_albums.length > 0) {
                        setTimeout(() => {
                            ElMessage.info(`${data.album_count}个相册已移到未分组`);
                        }, 500);
                    }
                }
            } catch (error) {
                if (error === 'cancel') {
                    // 用户取消
                    return;
                }
                ElMessage.error('删除分组失败');
            }
        };


        const handleGroupCommand = (command) => {
            if (command.action === 'edit') {
                editGroup(command.group);
            } else if (command.action === 'delete') {
                deleteGroup(command.group);
            } else if (command.action === 'collapse') {
                toggleGroupCollapse(command.group.id);
            }
        };

        const getGroupAlbumCount = (groupId) => {
            const group = albumGroups.value.find(g => g.id === groupId);
            return group ? group.album_count : 0;
        };


        // 修改加载相册分组信息的函数（编辑相册时调用）
        const loadAlbumGroupsInfo = async (albumId) => {
            try {
                const response = await fetch(`/api/albums/${albumId}/groups`);
                if (response.ok) {
                    const groups = await response.json();
                    // 设置当前相册的分组ID
                    if (currentAlbum.value) {
                        currentAlbum.value.group_ids = groups.map(g => g.id);
                    }
                }
            } catch (error) {
                console.error('加载相册分组失败:', error);
            }
        };

        // 在打开编辑相册对话框时加载分组信息
        watch(showEditAlbumDialog, (newVal) => {
            if (newVal && currentAlbum.value && currentAlbum.value.id) {
                loadAlbumGroupsInfo(currentAlbum.value.id);
            }
        });


        // filter start 20250115


        const currentFilter = ref('all'); // 'all', 'favorited', 'not_favorited'

        // 筛选选项
        const filterOptions = [
            {label: '全部图片', value: 'all'},
            {label: '已收藏', value: 'favorited'},
            {label: '未收藏', value: 'not_favorited'}
        ];

        // 改变筛选条件
        const changeFilter = (filterValue) => {
            currentFilter.value = filterValue;
            // 清空选择状态
            selectedImages.value = [];
        };

        // 修改 filteredImages 计算属性，同时考虑筛选和排序
        const filteredImages = computed(() => {
            let result;
            const isSmartResult = currentView.value === 'smart-album' || (currentImage.value && currentImage.value.smart_album_id);
            if (isSmartResult) {
                result = smartAlbumImages.value;
            } else if (currentView.value === 'library-share' || (currentImage.value && currentImage.value.source_type === 'library-share')) {
                result = shareImages.value;
            } else if (currentView.value === 'library' || (currentImage.value && currentImage.value.source_type === 'library')) {
                result = libraryImages.value;
            } else {
                result = images.value;
            }

            // 应用筛选
            if (currentFilter.value === 'favorited') {
                result = result.filter(img => img.is_favorited);
            } else if (currentFilter.value === 'not_favorited') {
                result = result.filter(img => !img.is_favorited);
            }

            // Smart Album keeps Python result order as a first-class option,
            // while ordinary display sorting stays in the Gallery UI.
            if (isSmartResult) {
                const smartResult = [...result];
                if (smartAlbumSort.value.field === 'query_order') {
                    return smartResult.sort((a, b) =>
                        (a.smart_query_order ?? 0) - (b.smart_query_order ?? 0)
                    );
                }

                if (smartAlbumSort.value.field === 'capture_time') {
                    return smartResult.sort((a, b) => {
                        const timeA = a.capture_sort_time ? new Date(a.capture_sort_time).getTime() : null;
                        const timeB = b.capture_sort_time ? new Date(b.capture_sort_time).getTime() : null;
                        const validA = Number.isFinite(timeA);
                        const validB = Number.isFinite(timeB);

                        // Missing/invalid dates are always placed last.
                        if (!validA && !validB) {
                            return (a.smart_query_order ?? 0) - (b.smart_query_order ?? 0);
                        }
                        if (!validA) return 1;
                        if (!validB) return -1;

                        if (timeA !== timeB) {
                            return smartAlbumSort.value.order === 'desc'
                                ? timeB - timeA
                                : timeA - timeB;
                        }
                        return (a.smart_query_order ?? 0) - (b.smart_query_order ?? 0);
                    });
                }

                return smartResult;
            }

            // 应用排序（复制数组避免修改原数组）
            result = [...result].sort((a, b) => {
                let valueA, valueB;

                switch (currentSort.value.field) {
                    case 'original_filename':
                        valueA = a.original_filename.toLowerCase();
                        valueB = b.original_filename.toLowerCase();
                        break;
                    case 'file_size':
                        valueA = a.file_size || 0;
                        valueB = b.file_size || 0;
                        break;
                    case 'uploaded_at':
                    default:
                        valueA = new Date(a.uploaded_at).getTime();
                        valueB = new Date(b.uploaded_at).getTime();
                        break;
                }

                // 处理null或undefined值
                if (valueA == null) valueA = '';
                if (valueB == null) valueB = '';

                let compareResult = 0;
                if (valueA < valueB) compareResult = -1;
                if (valueA > valueB) compareResult = 1;

                // 根据排序顺序调整
                return currentSort.value.order === 'desc' ? -compareResult : compareResult;
            });

            return result;
        });

        // 获取当前筛选标签
        const getCurrentFilterLabel = computed(() => {
            const option = filterOptions.find(opt => opt.value === currentFilter.value);
            return option ? option.label : '筛选方式';
        });

        // filter end

        //overlay start
        const showImageOverlay = ref(false);
        const overlayImageSrc = ref('');
        const overlayImageAlt = ref('');

// 添加单击图片事件处理函数
        const openImageOverlay = () => {
            if (currentImage.value && currentImage.value.id) {
                overlayImageSrc.value = detailImageUrl(currentImage.value, 'compressed');
                overlayImageAlt.value = currentImage.value.original_filename;
                showImageOverlay.value = true;
            }
        };

// 关闭悬浮层
        const closeImageOverlay = () => {
            showImageOverlay.value = false;
            overlayImageSrc.value = '';
            overlayImageAlt.value = '';
        };

// 添加键盘事件监听
        const handleKeyDown = (event) => {
            if (showImageOverlay.value) {
                if (event.key === 'Escape') {
                    closeImageOverlay();
                } else if (event.key === 'ArrowLeft') {
                    prevImage();
                } else if (event.key === 'ArrowRight') {
                    nextImage();
                }
            }
        };

        //overlay end

        //admin start


// 在 setup() 中添加


        // ... 您的现有代码 ...

        // 覆盖fetch函数
        const originalFetch = window.fetch;

        window.fetch = async function (url, options = {}) {
            const newOptions = {...options};
            newOptions.headers = newOptions.headers || {};

            // 如果是管理员且有token
            if (isAdmin.value && adminToken.value) {

                // 处理不同的headers类型
                if (newOptions.headers instanceof Headers) {
                    newOptions.headers.set('X-Admin-Token', adminToken.value);
                } else if (newOptions.headers && typeof newOptions.headers === 'object') {
                    newOptions.headers['X-Admin-Token'] = adminToken.value;
                } else {
                    newOptions.headers = {
                        'X-Admin-Token': adminToken.value
                    };
                }
            }

            return originalFetch.call(this, url, newOptions);
        };


        const isAdmin = ref(false);
        const adminToken = ref('');  // 添加这行
        const showAdminLogin = ref(false);
        const adminPassword = ref('');
        const adminLoginLoading = ref(false);

// 管理员登录验证
        const verifyAdminPassword = async () => {
            if (!adminPassword.value.trim()) {
                ElMessage.warning('请输入管理员密码');
                return;
            }

            adminLoginLoading.value = true;
            try {
                const response = await fetch('/api/admin/verify-password', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: adminPassword.value})
                });

                if (response.ok) {
                    const data = await response.json();
                    isAdmin.value = true;
                    adminToken.value = data.token;
                    localStorage.setItem('admin_token', data.token);
                    // localStorage.setItem('admin_token_expires', Date.now() + (data.expires_in * 1000));
                    // localStorage.setItem('is_admin', 'true');
                    showAdminLogin.value = false;
                    adminPassword.value = '';
                    await loadLibrarySources();
                    await loadSmartViews();
                    ElMessage.success('管理员登录成功');
                } else {
                    const errorData = await response.json();
                    ElMessage.error(errorData.error || '密码错误');
                }
            } catch (error) {
                ElMessage.error('验证失败');
            } finally {
                adminLoginLoading.value = false;
            }
        };


// 管理员登出
        const adminLogout = async () => {
            try { await fetch('/api/admin/logout', {method: 'POST'}); } catch (e) {}
            isAdmin.value = false;
            adminToken.value = '';
            librarySources.value = [];
            currentLibrarySource.value = null;
            smartAlbums.value = [];
            currentSmartAlbum.value = {};
            smartAlbumImages.value = [];
            smartSets.value = [];
            currentSmartSet.value = {};
            smartSetResults.value = [];
            smartSetDirectoryItems.value = [];
            localStorage.removeItem('admin_token');
            ElMessage.success('已退出管理员模式');
        };

// 恢复管理员状态
        const restoreAdminStatus = async () => {
            const token = localStorage.getItem('admin_token');
            // const expires = localStorage.getItem('admin_token_expires');

            if (!token) {
                isAdmin.value = false; //false
                return;
            }


            // 验证token有效性（向后端验证）
            try {
                const response = await fetch('/api/admin/verify-token', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: token})
                });

                if (response.ok) {
                    const data = await response.json();
                    if (data.valid) {
                        adminToken.value = token;
                        isAdmin.value = true;
                    } else {
                        // token无效，清除存储
                        localStorage.removeItem('admin_token');
                        // localStorage.removeItem('admin_token_expires');
                        isAdmin.value = false;
                    }
                } else {
                    // 验证失败，清除存储
                    localStorage.removeItem('admin_token');
                    // localStorage.removeItem('admin_token_expires');
                    isAdmin.value = false;
                }
            } catch (error) {
                console.error('恢复管理员状态失败:', error);
                isAdmin.value = false;
            }
        };


        //admin end

        // config start

        const showConfigDialog = ref(false);
        const siteConfig = ref({});
        const configLoading = ref(false);

        // 加载站点配置
        const loadSiteConfig = async () => {
            configLoading.value = true;
            try {
                const response = await fetch('/api/site-config');

                if (response.ok) {
                    siteConfig.value = await response.json();
                    showExifOnHover.value = siteConfig.value.show_exif_on_hover === '1';
                }
            } catch (error) {
                console.error('加载配置失败:', error);
            } finally {
                configLoading.value = false;
            }
        };

        // 保存配置
        const saveSiteConfig = async () => {
            configLoading.value = true;
            try {
                const response = await fetch('/api/site-config', {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(siteConfig.value)
                });

                if (response.ok) {
                    ElMessage.success('配置保存成功');
                    showConfigDialog.value = false;
                    // 重新加载站点标题
                    await loadSiteTitle();
                    if (siteConfig.value.new_password) {
                        adminLogout() // 如果修改了密码登出
                    }

                    showExifOnHover.value = siteConfig.value.show_exif_on_hover === '1';

                } else {
                    const data = await response.json();
                    ElMessage.error(data.error || '保存失败');
                }
            } catch (error) {
                ElMessage.error('保存配置失败');
            } finally {
                configLoading.value = false;
            }
        };

        // 检查是否可以上传（用于控制上传按钮显示）
        const canUpload = computed(() => {
            // 管理员总是可以上传
            if (isAdmin.value) return true;

            // 游客检查配置
            return siteConfig.value.allow_guest_upload === '1';
        });

        // 修改 showConfigDialog 的监听
        watch(showConfigDialog, async (newVal) => {
            if (newVal && isAdmin.value) {
                // 打开对话框时加载配置
                await loadSiteConfig();
            }
        });


        //config end

        //share
        // 生成图片分享链接（可选）

        // 生成相册分享链接
        const generateAlbumShareUrl = (albumId) => {
            return `${window.location.origin}${window.location.pathname}?album=${albumId}`;
        };

        // 生成图片分享链接
        const generateImageShareUrl = (albumId, imageId) => {
            return `${window.location.origin}${window.location.pathname}?album=${albumId}&image=${imageId}`;
        };

        // 显示分享对话框
        const showShareDialog = (type = 'album', imageId = null) => {
            let shareUrl = '';
            let title = '';

            if (type === 'album' && currentAlbum.value.id) {
                shareUrl = generateAlbumShareUrl(currentAlbum.value.id);
                title = `分享相册`;
            } else if (type === 'image' && currentImage.value.id && currentAlbum.value.id) {
                shareUrl = generateImageShareUrl(currentAlbum.value.id, currentImage.value.id);
                title = `分享图片`;
            } else {
                ElMessage.warning('无法生成分享链接');
                return;
            }

            ElMessageBox.confirm(
                `${shareUrl}`,
                {
                    title: title,
                    confirmButtonText: '复制链接',
                    cancelButtonText: '关闭',
                    beforeClose: async (action, instance, done) => {
                        if (action === 'confirm') {
                            try {
                                await navigator.clipboard.writeText(shareUrl);
                                ElMessage.success('链接已复制到剪贴板');
                                done();
                            } catch (error) {
                                // 降级方案
                                const textArea = document.createElement('textarea');
                                textArea.value = shareUrl;
                                document.body.appendChild(textArea);
                                textArea.select();
                                document.execCommand('copy');
                                document.body.removeChild(textArea);
                                ElMessage.success('链接已复制到剪贴板');
                                done();
                            }
                        } else {
                            done();
                        }
                    }
                }
            ).catch(() => {
                // 用户点击取消
            });
        };

        // 图片浏览模式与文件夹浏览模式彼此独立。
        const viewMode = ref('grid'); // 'grid' 或 'waterfall'
        const folderViewModeStorageKey = 'photo_library.folderViewMode';
        let savedFolderViewMode = 'grid';
        try {
            const storedMode = window.localStorage.getItem(folderViewModeStorageKey);
            if (storedMode === 'grid' || storedMode === 'list') savedFolderViewMode = storedMode;
        } catch (_) {
            // localStorage 不可用时保持默认值，不影响目录浏览。
        }
        const folderViewMode = ref(savedFolderViewMode); // 'grid' 或 'list'；仅在当前层包含文件夹时显示切换
        watch(folderViewMode, (mode) => {
            if (mode !== 'grid' && mode !== 'list') return;
            try {
                window.localStorage.setItem(folderViewModeStorageKey, mode);
            } catch (_) {
                // 持久化失败只影响偏好记忆，不影响当前会话。
            }
        });


        //exif waterfall
        // 在setup()中添加
        const exifCache = ref({});


        const exifLoading = ref({}); // 加载状态
        const exifTimers = ref({}); // 定时器，防止频繁请求

        // 在setup()中添加
        const showExifOnHover = ref(true); // 默认显示


        // 悬停时加载EXIF
        const loadExifOnHover = async (imageOrId) => {

            if (!showExifOnHover.value) return;

            const image = (typeof imageOrId === 'object' && imageOrId !== null)
                ? imageOrId
                : filteredImages.value.find(img => img.id === imageOrId) || images.value.find(img => img.id === imageOrId);
            if (!image) return;

            const cacheKey = image.id;
            if (exifCache.value[cacheKey] !== undefined) return;

            exifLoading.value[cacheKey] = true;
            exifTimers.value[cacheKey] = setTimeout(async () => {
                try {
                    let url;
                    if (image.source_type === 'library') {
                        url = `/api/library/sources/${image.source_id}/exif?path=${encodeURIComponent(image.relative_path)}`;
                    } else if (image.source_type === 'library-share') {
                        url = `/api/library/shares/${encodeURIComponent(image.share_token || currentShareToken.value)}/exif?path=${encodeURIComponent(image.relative_path)}`;
                    } else {
                        url = `/api/images/${image.id}/exif`;
                    }
                    const response = await fetch(url);
                    if (response.ok) {
                        const data = await response.json();
                        exifCache.value[cacheKey] = data.exif || null;
                    } else {
                        exifCache.value[cacheKey] = null;
                    }
                } catch (error) {
                    console.error('加载EXIF信息失败:', error);
                    exifCache.value[cacheKey] = null;
                } finally {
                    exifLoading.value[cacheKey] = false;
                    delete exifTimers.value[cacheKey];
                }
            }, 300);
        };

        // 清除定时器
        const clearExifTimer = (imageId) => {
            if (exifTimers.value[imageId]) {
                clearTimeout(exifTimers.value[imageId]);
                delete exifTimers.value[imageId];
            }

            // 如果还在加载中，清除加载状态
            if (exifLoading.value[imageId]) {
                setTimeout(() => {
                    exifLoading.value[imageId] = false;
                }, 100);
            }
        };

        // 预加载并缓存EXIF信息
        const cacheImageExif = async (imageId) => {
            // 避免重复请求
            if (exifCache.value[imageId]) return;

            try {
                const response = await fetch(`/api/images/${imageId}/exif`);
                if (response.ok) {
                    const data = await response.json();
                    exifCache.value[imageId] = data.exif || {};
                }
            } catch (error) {
                console.error('加载EXIF信息失败:', error);
                exifCache.value[imageId] = {};
            }
        };

        // 检查是否有需要的EXIF信息
        const hasExifInfo = (imageId) => {
            const exif = exifCache.value[imageId];
            if (!exif) return false;

            return exif.ISO;
        };

        // 获取格式化后的EXIF值
        // 获取EXIF值
        const getExifValue = (imageId, key) => {
            const exif = exifCache.value[imageId];
            if (!exif) return '';

            const value = exif[key];
            if (value === undefined || value === null) return '';

            return String(value);
        };


        //collasp
// 在setup()中添加
        const collapsedGroups = ref(new Set());

// 切换分组折叠状态
        const toggleGroupCollapse = (groupId) => {
            if (collapsedGroups.value.has(groupId)) {
                collapsedGroups.value.delete(groupId);
            } else {
                collapsedGroups.value.add(groupId);
            }

            // 保存到localStorage
            saveCollapsedGroups();
        };

// 检查分组是否折叠
        const isGroupCollapsed = (groupId) => {
            return collapsedGroups.value.has(groupId);
        };

// 保存折叠状态到localStorage
        const saveCollapsedGroups = () => {
            const collapsedArray = Array.from(collapsedGroups.value);
            localStorage.setItem('collapsed_groups', JSON.stringify(collapsedArray));
        };

// 从localStorage恢复折叠状态
        const loadCollapsedGroups = () => {
            try {
                const saved = localStorage.getItem('collapsed_groups');
                if (saved) {
                    const collapsedArray = JSON.parse(saved);
                    collapsedGroups.value = new Set(collapsedArray);
                }
            } catch (error) {
                console.error('加载折叠状态失败:', error);
            }
        };

        const resetNewAlbumForm = (showDialog = true) => {
            newAlbum.value = {
                type: 'uploaded',
                name: '',
                description: '',
                shoot_date: '',
                model_name: '',
                location: '',
                group_ids: [],
            };
            if (showDialog) showCreateAlbumDialog.value = true;
        };

// 显示在特定分组中创建相册
        const showCreateAlbumInGroup = (group) => {
            resetNewAlbumForm(false);
            // Group shortcuts always create a traditional uploaded album.
            newAlbum.value.type = 'uploaded';
            newAlbum.value.group_ids = group.is_ungrouped ? [] : [group.id];
            showCreateAlbumDialog.value = true;
        };


        const resetAlbumDialog = () => {
            resetNewAlbumForm(true);
        };


        return {
            currentView,
            exploreStats,
            exploreLoading,
            exploreQueryLoading,
            exploreError,
            exploreSourceText,
            exploreSelectedYear,
            exploreYearOptions,
            exploreMonthRows,
            exploreExpandedSections,
            visibleExploreRows,
            exploreCanExpand,
            exploreHiddenCount,
            toggleExploreSection,
            formatExplorePercent,
            openExplore,
            loadExploreStats,
            openExploreStat,
            backToExplore,
            smartViews,
            showCreateSmartViewDialog,
            smartViewCreateEditor,
            openCreateSmartView,
            changeSmartViewCreateType,
            openSmartViewCreateHelp,
            createSmartView,
            openSmartView,
            smartViewResultText,
            handleSmartViewCommand,
            loadSmartViews,
            smartAlbums,
            currentSmartAlbum,
            smartAlbumImages,
            smartAlbumLoading,
            smartAlbumIndexRefreshing,
            smartAlbumIndexProgress,
            smartAlbumIndex,
            smartAlbumIndexSelectedSourceIds,
            smartAlbumSyncSelectedSourceIds,
            smartAlbumSyncScanning,
            smartAlbumSyncActive,
            smartAlbumSyncPlan,
            smartAlbumSyncProgress,
            showSmartAlbumSyncDialog,
            enabledSmartAlbumIndexSources,
            smartAlbumIndexedSourceText,
            showSmartAlbumIndexDialog,
            showSmartAlbumHelpDialog,
            smartAlbumHelpLoading,
            smartAlbumRuntime,
            smartAlbumStepStatusText,
            smartAlbumStepTagType,
            smartAlbumStepPercent,
            smartAlbumStepSummary,
            formatSmartAlbumStageCounts,
            smartAlbumQueryError,
            showEditSmartAlbumDialog,
            smartAlbumEditor,
            smartSets,
            currentSmartSet,
            smartSetResults,
            smartSetDirectoryItems,
            smartSetLoading,
            smartSetQueryError,
            showEditSmartSetDialog,
            showSmartSetHelpDialog,
            smartSetHelpLoading,
            smartSetRuntime,
            smartSetEditor,
            loadSmartSets,
            openSmartSet,
            runSmartSet,
            editSmartSet,
            saveSmartSet,
            deleteSmartSet,
            openSmartSetHelp,
            openSmartSetResult,
            loadSmartAlbums,
            openSmartAlbum,
            runSmartAlbum,
            refreshSmartAlbumIndex,
            startSmartAlbumIndexRefresh,
            cancelSmartAlbumIndexRefresh,
            openSmartAlbumIndexSync,
            scanSmartAlbumIndexChanges,
            startSmartAlbumIndexSync,
            cancelSmartAlbumIndexSync,
            invalidateSmartAlbumSyncPlan,
            openSmartAlbumHelp,
            editSmartAlbum,
            saveSmartAlbum,
            deleteSmartAlbum,
            librarySources,
            currentLibrarySource,
            libraryListing,
            libraryLoading,
            allLibraryDirectories,
            libraryDirectories,
            setSearchQuery,
            libraryImages,
            libraryStats,
            libraryFavoriteCount,
            libraryEmptyMessage,
            showLibrarySourcesDialog,
            newLibrarySource,
            showNewSetDialog,
            newSetCreating,
            newSetForm,
            newSetModelOptions,
            setSortOrder,
            manifestArrayVisible,
            manifestArrayLoading,
            manifestArrayText,
            manifestArrayCount,
            openManifestArray,
            removeLibraryDotfiles,
            openFolderCompareTool,
            copyManifestArray,
            openNewSetDialog,
            createNewSet,
            loadLibrarySources,
            addLibrarySource,
            renameLibrarySource,
            deleteLibrarySource,
            setLibrarySourceEnabled,
            openLibrarySource,
            openLibraryDirectory,
            openSetParentDirectory,
            libraryBack,
            libraryAssetUrl,
            libraryPathAssetUrl,
            libraryDirectoryCoverUrl,
            detailImageUrl,
            viewLibraryImage,
            toggleCurrentFavorite,
            softDeleteCurrentLibraryImage,
            toggleImageFavorite,
            exportLibraryFavorites,
            manifestSummary,
            isSetDirectory,
            isLibraryRoot,
            setStats: setInsights.setStats,
            setStatsLoading: setInsights.statsLoading,
            setStatsError: setInsights.statsError,
            equipmentInfo: setInsights.equipment,
            equipmentState: setInsights.equipmentState,
            equipmentError: setInsights.equipmentError,
            equipmentCameraText: setInsights.cameraText,
            equipmentLensText: setInsights.lensText,
            equipmentFocalText: setInsights.focalText,
            rescanEquipment: setInsights.rescanEquipment,
            validationVisible: setInsights.validationVisible,
            validationRulesVisible: setInsights.validationRulesVisible,
            validationLoading: setInsights.validationLoading,
            validationData: setInsights.validationData,
            validationError: setInsights.validationError,
            validationSortMode: setInsights.validationSortMode,
            validationSearchQuery: setInsights.validationSearchQuery,
            validationSortedSets: setInsights.sortedValidationSets,
            validationStatusLabel: setInsights.validationStatusLabel,
            validationStatusType: setInsights.validationStatusType,
            validationCountClass: setInsights.validationCountClass,
            openLibraryValidation: setInsights.openValidation,
            workflowPreviewVisible: workflowTools.previewVisible,
            workflowPreviewLoading: workflowTools.previewLoading,
            workflowPreviewKind: workflowTools.previewKind,
            workflowPreviewTitle: workflowTools.previewTitle,
            workflowPreviewData: workflowTools.previewData,
            workflowThreshold: workflowTools.threshold,
            workflowProtectOriginalsLabel: workflowTools.protectOriginalsLabel,
            handleWorkflowMenuVisible: workflowTools.handleWorkflowMenuVisible,
            imageInspectionVisible: workflowTools.inspectionVisible,
            imageInspectionRulesVisible: workflowTools.inspectionRulesVisible,
            imageInspectionLoading: workflowTools.inspectionLoading,
            imageInspectionData: workflowTools.inspectionData,
            imageInspectionError: workflowTools.inspectionError,
            inspectionMetadataVisible: workflowTools.inspectionMetadataVisible,
            inspectionMetadataLoading: workflowTools.inspectionMetadataLoading,
            inspectionMetadataData: workflowTools.inspectionMetadataData,
            inspectionMetadataError: workflowTools.inspectionMetadataError,
            photoImportVisible: workflowTools.photoImportVisible,
            photoImportLoading: workflowTools.photoImportLoading,
            photoImportExecuting: workflowTools.photoImportExecuting,
            photoImportForm: workflowTools.photoImportForm,
            photoImportPlan: workflowTools.photoImportPlan,
            photoImportSelectedGroups: workflowTools.photoImportSelectedGroups,
            previewPhotoImport: workflowTools.previewPhotoImport,
            openPhotoImportTool: workflowTools.openPhotoImport,
            executePhotoImport: workflowTools.executePhotoImport,
            photoImportThumbnailUrl: workflowTools.photoImportThumbnailUrl,
            photoImportGroupTime: workflowTools.photoImportGroupTime,
            schedulePhotoImportSourceRootSave: workflowTools.schedulePhotoImportSourceRootSave,
            workflowProgressVisible: workflowTools.progressVisible,
            workflowTask: workflowTools.task,
            workflowCanExecute: workflowTools.canExecute,
            openImageInspectionTool: workflowTools.openImageInspection,
            openInspectionMetadata: workflowTools.openInspectionMetadata,
            openVisualRenameTool: workflowTools.openVisualRename,
            rerunVisualRenamePreview: workflowTools.analyzeVisualRename,
            openDiscardUnreturnedBaseTool: workflowTools.openDiscardUnreturnedBase,
            openProtectOriginalsTool: workflowTools.openProtectOriginals,
            openSyncRawByJpgTool: workflowTools.openSyncRawByJpg,
            openSyncJpgByRawTool: workflowTools.openSyncJpgByRaw,
            openSelectRawTool: workflowTools.openSelectRaw,
            startWorkflowOperation: workflowTools.startCurrent,
            closeWorkflowProgress: workflowTools.closeProgress,
            workflowStatusLabel: workflowTools.statusLabel,
            workflowStatusType: workflowTools.statusType,
            workflowFormatSize: workflowTools.formatSize,
            finalBuilderRulesVisible,
            finalMetadataRulesVisible,
            finalBuilderVisible: finalBuilder.visible,
            finalBuilderLoading: finalBuilder.loading,
            finalBuilderStep: finalBuilder.step,
            finalBuilderSelection: finalBuilder.selection,
            finalBuilderSelectedIds: finalBuilder.selectedIds,
            finalBuilderSelectedCount: finalBuilder.selectedCount,
            finalBuilderPlan: finalBuilder.plan,
            finalBuilderTask: finalBuilder.task,
            finalBuilderCanPreview: finalBuilder.canPreview,
            finalBuilderCanExecute: finalBuilder.canExecute,
            openFinalBuilder: finalBuilder.open,
            closeFinalBuilder: finalBuilder.close,
            finalBuilderIsSelected: finalBuilder.isSelected,
            setFinalBuilderSelected: finalBuilder.setSelected,
            selectFinalBuilderGroup: finalBuilder.selectGroup,
            finalBuilderThumbnailUrl: finalBuilder.thumbnailUrl,
            previewFinalBuilder: finalBuilder.preview,
            backFinalBuilderSelection: finalBuilder.backToSelection,
            executeFinalBuilder: finalBuilder.execute,
            continueFinalBuilderMetadata: finalBuilder.continueToMetadata,
            finalBuilderStatusType: finalBuilder.statusType,
            finalBuilderStatusText: finalBuilder.statusText,
            finalMetadataVisible: finalMetadata.visible,
            finalMetadataLoading: finalMetadata.loading,
            finalMetadataStep: finalMetadata.step,
            finalMetadataPlan: finalMetadata.plan,
            finalMetadataTask: finalMetadata.task,
            finalMetadataCanExecute: finalMetadata.canExecute,
            finalMetadataSelectedRowIds: finalMetadata.selectedRowIds,
            finalMetadataSelectedCount: finalMetadata.selectedCount,
            finalMetadataAllRowsSelected: finalMetadata.allRowsSelected,
            finalMetadataSomeRowsSelected: finalMetadata.someRowsSelected,
            finalMetadataIsRowSelected: finalMetadata.isRowSelected,
            setFinalMetadataRowSelected: finalMetadata.setRowSelected,
            setAllFinalMetadataRowsSelected: finalMetadata.setAllRowsSelected,
            openFinalMetadata: finalMetadata.open,
            closeFinalMetadata: finalMetadata.close,
            refreshFinalMetadataPlan: finalMetadata.refreshPlan,
            finalMetadataThumbnailUrl: finalMetadata.thumbnailUrl,
            executeFinalMetadata: finalMetadata.execute,
            currentManifestData,
            formatEnumValue,
            formatManifestValue,
            hasManifestValue,
            hasManifestProps,
            formatSceneValue,
            formatWeatherValue,
            manifestShootDuration,
            currentManifestLightCount,
            manifestVenuePaid,
            formatLibraryFolderCounts,
            showManifestDialog,
            showManifestDetailDialog,
            manifestForm,
            manifestEditorMode,
            manifestJsonText,
            manifestJsonError,
            manifestOptions,
            manifestKnownValues,
            manifestHistoryOptions,
            resetManifestDetailScroll,
            resetManifestEditorScroll,
            queryKnownLocations,
            selectKnownLocation,
            syncKnownLocation,
            queryKnownSources,
            selectKnownSource,
            syncKnownSource,
            queryKnownModels,
            queryKnownThemeNames,
            queryKnownCharacters,
            queryKnownVariants,
            queryKnownReferences,
            queryKnownOutfits,
            queryKnownPrimaryPhotographers,
            queryKnownFixtures,
            queryKnownLightingNotes,
            gpsImporting: gpsPhotoImport.loading,
            importGpsFromPhoto: gpsPhotoImport.run,
            switchManifestEditorMode,
            syncManifestJsonFromForm,
            syncManifestFormFromJson,
            formatManifestJson,
            openManifestDetails,
            openManifestEditor,
            editManifestFromDetail,
            jumpToRelatedSet,
            addAdditionalSession,
            removeAdditionalSession,
            addLightingRow,
            removeLightingRow,
            saveManifest,
            showLibraryShareDialog,
            libraryShareForm,
            openLibraryShareDialog,
            createLibraryShare,
            currentShareToken,
            libraryShare,
            shareNeedsPassword,
            sharePassword,
            shareLoading,
            shareDirectories,
            shareImages,
            shareEmptyMessage,
            shareSelectedImages,
            unlockLibraryShare,
            openLibraryShareDirectory,
            libraryShareBack,
            sharePathAssetUrl,
            shareAssetUrl,
            toggleShareSelection,
            exportShareSelection,
            albums,
            images,
            currentAlbum,
            currentImage,
            showCreateAlbumDialog,
            showEditAlbumDialog,
            showUploadDialog,
            newAlbum,
            currentImageIndex,
            detailImageList,
            hasPrev,
            hasNext,
            getAlbumImageCount,
            loadAlbums,
            loadAlbumImages,
            createAlbum,
            updateAlbum,
            deleteAlbum,
            openAlbum,
            backToAlbums,
            backToAlbum,
            viewImage,
            prevImage,
            nextImage,
            handleUploadSuccess,
            handleUploadError,
            beforeUpload,
            deleteImage,
            setAsCover,
            downloadImage,
            formatDate,
            formatFileSize,
            editImageFilename,
            renameImageFile,
            toggleFavorite,
            selectionMode,
            selectedImages,
            toggleSelectionMode,
            handleImageClick,
            batchDeleteImages,
            selectAllImages,
            isAllSelected,
            filteredImages,
            passwordEnabled,
            newPassword,
            handlePasswordToggle,
            showPasswordDialog,
            checkAlbumPasswordStatus,
            setAlbumPassword,
            removeAlbumPassword,
            checkAlbumAccess,
            editImageDescription,
            showExifDialog,
            exifData,
            exifTableData,
            showImageExif,
            siteTitle,
            loadSiteTitle,
            editSiteTitle,
            saveSiteTitle,
            showMoveToAlbumDialog,
            targetAlbumId,
            otherAlbums,
            showMoveDialog,
            moveSelectedImages,
            getFavoriteCount,
            exportFavoriteList,
            albumGroups,
            ungroupedAlbums,
            allGroups,
            showManageGroupsDialog,
            showEditGroupDialog,
            newGroupName,
            groupForm,
            editingGroup,
            createGroup,
            editGroup,
            saveGroup,
            deleteGroup,
            handleGroupCommand,
            getGroupAlbumCount,
            currentSort,
            changeSort,
            sortOptions,
            getCurrentSortLabel,
            smartAlbumSort,
            smartAlbumSortOptions,
            changeSmartAlbumSort,
            getCurrentSmartAlbumSortLabel,
            restoreAlbumAccessTokens,
            currentFilter,
            filterOptions,
            changeFilter,
            getCurrentFilterLabel,
            showImageOverlay,
            overlayImageSrc,
            overlayImageAlt,
            openImageOverlay,
            closeImageOverlay,
            verifyAdminPassword,
            isAdmin,
            restoreAdminStatus,
            adminLogout,
            showAdminLogin,
            adminPassword,
            adminLoginLoading,

            // 新增的配置管理导出
            showConfigDialog,
            siteConfig,
            configLoading,
            loadSiteConfig,
            saveSiteConfig,
            canUpload,

            showShareDialog,
            generateAlbumShareUrl,
            generateImageShareUrl, viewMode, folderViewMode, librarySelectedDirectoryPath,


            cacheImageExif,
            hasExifInfo,
            getExifValue,
            exifCache,

            // EXIF相关
            loadExifOnHover,
            clearExifTimer,
            exifLoading,
            // EXIF显示配置
            showExifOnHover,

            toggleGroupCollapse,
            isGroupCollapsed,
            showCreateAlbumInGroup,
            resetAlbumDialog


        };
    }
})

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component)
}
app.use(ElementPlus).mount('#app');