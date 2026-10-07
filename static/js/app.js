import {createController as createUploadedAlbumUpload} from './features/uploaded_albums/upload.js';
import {createController as createGpsPhotoImporter} from './features/mapped_library/gps_import.js';
import {createController as createManifestAutofill} from './features/mapped_library/manifest_autofill.js';
import {createController as createSetInfo} from './features/mapped_library/set_info.js';
import {createController as createWorkflowTools} from './features/workflow/tools.js';
import {createController as createValidation} from './features/workflow/validate.js';
import {createController as createFinalBuilder} from './features/workflow/final/builder.js';
import {createController as createFinalMetadata} from './features/workflow/final/metadata.js';
import {SmartAlbumApi} from './features/smart/albums.js';
import {SmartSetApi} from './features/smart/sets.js';
import {ExploreApi} from './features/smart/explore.js';
import {SmartHelpersApi} from './features/smart/helpers.js';

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

        // ==================== Feature: Smart views / Explore ====================
        const SMART_ALBUM_DEFAULT_CODE = `result = [
    photo
    for photo in photos
    if photo.state.favorite
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
            phase: 'idle',
            message: '',
            overall: {dimension: 'image', label: 'Overall', current: 0, total: 0, percent: 0, ready: false},
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
        const smartAlbumQueryError = ref('');
        const smartAlbumSort = ref({field: 'query_order', order: 'asc'});
        const smartAlbumSortOptions = [
            {label: 'Query Order', value: {field: 'query_order', order: 'asc'}},
            {label: 'Taken · Newest', value: {field: 'capture_time', order: 'desc'}},
            {label: 'Taken · Oldest', value: {field: 'capture_time', order: 'asc'}},
        ];
        const showSmartAlbumIndexDialog = ref(false);
        const showSmartAlbumIndexHelpDialog = ref(false);
        const smartAlbumIndexTab = ref('rebuild');
        const smartAlbumIndexDialogBusy = computed(() =>
            smartAlbumIndexRefreshing.value || smartAlbumSyncActive.value || smartAlbumSyncScanning.value
        );
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

        const EXPLORE_BLOCK_DEFAULT_CODE = `selected = helpers.preferred_versions(photos)

result = group_sets(
    sets,
    key=lambda item: item.manifest.model,
    include_missing=True,
    photo_scope=selected,
)
`;
        const emptyExploreStats = () => ({
            total_images: 0,
            total_sets: 0,
            sources: [],
            years: [],
            models: [],
            themes: [],
            locations: [],
            custom_blocks: [],
            index: null,
        });
        const exploreStats = ref(emptyExploreStats());
        const exploreLoading = ref(false);
        const exploreStatsLoaded = ref(false);
        const exploreQueryLoading = ref(false);
        const exploreError = ref('');
        const exploreSelectedYear = ref(null);
        const exploreMetricModes = ref({
            model: 'sets',
            year_month: 'sets',
            theme: 'sets',
            location: 'sets',
        });
        const EXPLORE_CARD_ITEMS = 6;
        const exploreMoreDialogVisible = ref(false);
        const exploreMoreDialog = ref({
            title: '',
            dimension: '',
            primary_target: 'sets',
            year: null,
            block_id: null,
            count_only: false,
            rows: [],
        });
        const exploreBlocks = ref([]);
        const exploreBlockDragId = ref(null);
        const exploreBlockDragOverId = ref(null);
        const showExploreBlockDialog = ref(false);
        const exploreBlockSaving = ref(false);
        const exploreBlockPreviewLoading = ref(false);
        const exploreBlockPreview = ref(null);
        const exploreBlockEditor = ref({
            id: null,
            name: '',
            description: '',
            display_mode: 'both',
            enabled: true,
            python_code: EXPLORE_BLOCK_DEFAULT_CODE,
        });
        const showExploreBlockHelpDialog = ref(false);
        const exploreBlockHelpLoading = ref(false);
        const exploreBlockRuntime = ref(null);

        const showSmartHelpersDialog = ref(false);
        const smartHelpersLoading = ref(false);
        const smartHelpersSaving = ref(false);
        const smartHelpersValidating = ref(false);
        const smartHelpersSource = ref('');
        const smartHelpersFunctions = ref([]);
        const smartHelpersError = ref('');
        const smartHelpersPath = ref('data/custom_helpers.py');

        const exploreFullscreenLoading = computed(() => (
            exploreLoading.value
            || exploreQueryLoading.value
            || exploreBlockPreviewLoading.value
            || exploreBlockSaving.value
            || exploreBlockHelpLoading.value
        ));
        let exploreReturnScrollY = 0;


        const detailImageList = computed(() => filteredImages.value);

        const currentImageIndex = computed(() => {
            return detailImageList.value.findIndex(img => img.id === currentImage.value.id);
        });


        const hasPrev = computed(() => currentImageIndex.value > 0);
        const hasNext = computed(() => currentImageIndex.value < detailImageList.value.length - 1);

        const prevImage = () => {
            if (!hasPrev.value) {
                ElMessage.info('First photo.');
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
                ElMessage.info('Last photo.');
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

                // Keep the synthetic Ungrouped entry on the home page.
                albumGroups.value = allData;

                // Exclude the synthetic Ungrouped entry from selectable groups.
                allGroups.value = allData.filter(group => !group.is_ungrouped);


            } catch (error) {
                ElMessage.error('Failed to load albums.');
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
                ElMessage.error('Album not found.');
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
                ElMessage.error('Album not found.');
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
                                    ElMessage.warning('Photo not found.');
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
                            ElMessage.warning('Photo not found.');
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
                    throw new Error('Load failed.');
                }

                images.value = await response.json();
                sortImages();

                return true;
            } catch (error) {
                ElMessage.error('Failed to load photos.');
                return false;
            }
        };


        const loadSmartAlbums = async () => {
            if (!isAdmin.value || !SmartAlbumApi) {
                smartAlbums.value = [];
                return;
            }
            try {
                const data = await SmartAlbumApi.list();
                smartAlbums.value = Array.isArray(data.albums) ? data.albums : [];
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                console.error('Failed to load Smart Album:', error);
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
        const exploreBlockDimension = blockId => `block:${blockId}`;
        const normalizeExploreBlockDisplayMode = block => {
            const mode = String(block?.display_mode || 'both');
            return ['sets', 'photos', 'both', 'sets_count_only'].includes(mode) ? mode : 'both';
        };
        const exploreBlockDefaultTarget = block => normalizeExploreBlockDisplayMode(block) === 'photos' ? 'photos' : 'sets';
        const exploreBlockHasMetricSelect = block => normalizeExploreBlockDisplayMode(block) === 'both';
        const exploreBlockCountOnly = block => normalizeExploreBlockDisplayMode(block) === 'sets_count_only';
        const exploreBlockMetricTarget = block => {
            const mode = normalizeExploreBlockDisplayMode(block);
            if (mode === 'photos') return 'photos';
            if (mode !== 'both') return 'sets';
            return exploreMetricModes.value[exploreBlockDimension(block.id)] === 'photos' ? 'photos' : 'sets';
        };
        const ensureExploreBlockModes = blocks => {
            for (const block of (Array.isArray(blocks) ? blocks : [])) {
                const dimension = exploreBlockDimension(block.id);
                const mode = normalizeExploreBlockDisplayMode(block);
                if (mode === 'photos') {
                    exploreMetricModes.value[dimension] = 'photos';
                } else if (mode !== 'both') {
                    exploreMetricModes.value[dimension] = 'sets';
                } else if (!['sets', 'photos'].includes(exploreMetricModes.value[dimension])) {
                    exploreMetricModes.value[dimension] = 'sets';
                }
            }
        };
        const resetExploreBlockModes = blocks => {
            for (const block of (Array.isArray(blocks) ? blocks : [])) {
                exploreMetricModes.value[exploreBlockDimension(block.id)] = exploreBlockDefaultTarget(block);
            }
        };
        const sortExploreBlockItems = items => (Array.isArray(items) ? items : [])
            .slice()
            .sort((a, b) => {
                const byOrder = Number(a?.display_order || 0) - Number(b?.display_order || 0);
                if (byOrder) return byOrder;
                return Number(a?.id || 0) - Number(b?.id || 0);
            });
        const applyExploreBlockOrder = blockIds => {
            const orderById = new Map((Array.isArray(blockIds) ? blockIds : []).map(
                (blockId, index) => [Number(blockId), (index + 1) * 10]
            ));
            const withOrder = items => sortExploreBlockItems((Array.isArray(items) ? items : []).map(item => {
                const displayOrder = orderById.get(Number(item?.id));
                return displayOrder == null ? item : {...item, display_order: displayOrder};
            }));
            exploreBlocks.value = withOrder(exploreBlocks.value);
            exploreStats.value = {
                ...exploreStats.value,
                custom_blocks: withOrder(exploreStats.value?.custom_blocks),
            };
        };
        const clearExploreBlockDrag = () => {
            exploreBlockDragId.value = null;
            exploreBlockDragOverId.value = null;
        };
        const startExploreBlockDrag = (block, event) => {
            const blockId = Number(block?.id || 0);
            if (!blockId) return;
            exploreBlockDragId.value = blockId;
            exploreBlockDragOverId.value = null;
            if (event?.dataTransfer) {
                event.dataTransfer.effectAllowed = 'move';
                event.dataTransfer.setData('text/plain', String(blockId));
            }
        };
        const overExploreBlockDrag = (block, event) => {
            const targetId = Number(block?.id || 0);
            if (!exploreBlockDragId.value || !targetId || targetId === Number(exploreBlockDragId.value)) {
                exploreBlockDragOverId.value = null;
                return;
            }
            if (event?.dataTransfer) event.dataTransfer.dropEffect = 'move';
            exploreBlockDragOverId.value = targetId;
        };
        const dropExploreBlock = async (block, event) => {
            const draggedId = Number(exploreBlockDragId.value || 0);
            const targetId = Number(block?.id || 0);
            const current = sortExploreBlockItems(exploreStats.value?.custom_blocks);
            const previousIds = current.map(item => Number(item.id));
            if (!draggedId || !targetId || draggedId === targetId || !previousIds.includes(draggedId)) {
                clearExploreBlockDrag();
                return;
            }

            const draggedIndex = current.findIndex(item => Number(item.id) === draggedId);
            const targetIndex = current.findIndex(item => Number(item.id) === targetId);
            if (draggedIndex < 0 || targetIndex < 0) {
                clearExploreBlockDrag();
                return;
            }

            // The Explore cards use a two-column grid. Treat dropping on another card
            // as an exact position swap so horizontal, vertical, and diagonal moves
            // never shift unrelated cards through the row-major list.
            const reordered = [...current];
            [reordered[draggedIndex], reordered[targetIndex]] = [
                reordered[targetIndex],
                reordered[draggedIndex],
            ];
            const nextIds = reordered.map(item => Number(item.id));
            if (nextIds.every((id, index) => id === previousIds[index])) {
                clearExploreBlockDrag();
                return;
            }

            applyExploreBlockOrder(nextIds);
            clearExploreBlockDrag();
            try {
                const data = await ExploreApi.reorderBlocks(nextIds);
                const persistedIds = sortExploreBlockItems(data?.blocks).map(item => Number(item.id));
                if (persistedIds.length === nextIds.length) applyExploreBlockOrder(persistedIds);
            } catch (error) {
                applyExploreBlockOrder(previousIds);
                ElMessage.error(error.message || 'Reorder failed.');
            }
        };
        const upsertExploreBlockDefinition = block => {
            if (!block?.id) return;
            const blockId = Number(block.id);
            const items = (Array.isArray(exploreBlocks.value) ? exploreBlocks.value : []).filter(
                item => Number(item?.id) !== blockId
            );
            items.push(block);
            exploreBlocks.value = sortExploreBlockItems(items);
            ensureExploreBlockModes([block]);
        };
        const disabledExploreBlockCard = block => ({
            id: block.id,
            name: block.name || '',
            description: block.description || '',
            display_mode: normalizeExploreBlockDisplayMode(block),
            display_order: Number(block.display_order || 0),
            enabled: false,
            presentation: block.presentation || 'list',
            source_kind: null,
            rows: [],
            error: '',
        });
        const upsertExploreBlockCard = card => {
            if (!card?.id) return;
            const blockId = Number(card.id);
            const items = (Array.isArray(exploreStats.value?.custom_blocks) ? exploreStats.value.custom_blocks : []).filter(
                item => Number(item?.id) !== blockId
            );
            items.push(card);
            exploreStats.value = {
                ...exploreStats.value,
                custom_blocks: sortExploreBlockItems(items),
            };
            ensureExploreBlockModes([card]);
        };
        const removeExploreBlockLocal = blockId => {
            const targetId = Number(blockId);
            exploreBlocks.value = (Array.isArray(exploreBlocks.value) ? exploreBlocks.value : []).filter(
                item => Number(item?.id) !== targetId
            );
            exploreStats.value = {
                ...exploreStats.value,
                custom_blocks: (Array.isArray(exploreStats.value?.custom_blocks) ? exploreStats.value.custom_blocks : []).filter(
                    item => Number(item?.id) !== targetId
                ),
            };
            delete exploreMetricModes.value[exploreBlockDimension(targetId)];
            if (Number(exploreMoreDialog.value?.block_id) === targetId) {
                exploreMoreDialogVisible.value = false;
            }
        };
        const loadExploreBlockStat = async blockId => {
            if (!ExploreApi || !blockId) return null;
            const data = await ExploreApi.blockStats(blockId);
            if (data?.card) upsertExploreBlockCard(data.card);
            if (data?.index) smartAlbumIndex.value = data.index;
            return data?.card || null;
        };

        const exploreMetricTarget = dimension => exploreMetricModes.value[dimension] === 'photos' ? 'photos' : 'sets';
        const exploreMetricCount = (item, dimension) => exploreMetricTarget(dimension) === 'photos'
            ? Number(item?.image_count || 0)
            : Number(item?.set_count || 0);
        const exploreMetricPercentage = (item, dimension) => exploreMetricTarget(dimension) === 'photos'
            ? Number(item?.image_percentage || 0)
            : Number(item?.set_percentage || 0);
        const exploreMetricUnit = dimension => exploreMetricTarget(dimension) === 'photos' ? 'Photos' : 'Sets';
        const exploreRowsByMetric = (rows, dimension) => (Array.isArray(rows) ? rows : [])
            .slice()
            .sort((a, b) => {
                const byCount = exploreMetricCount(b, dimension) - exploreMetricCount(a, dimension);
                if (byCount) return byCount;
                return String(a?.label || '').localeCompare(String(b?.label || ''), undefined, {numeric: true});
            });
        const visibleExploreRows = (rows, dimension) => exploreRowsByMetric(rows, dimension).slice(0, EXPLORE_CARD_ITEMS);
        const exploreHasMore = rows => Array.isArray(rows) && rows.length > EXPLORE_CARD_ITEMS;
        const exploreHiddenCount = rows => Math.max(0, (Array.isArray(rows) ? rows.length : 0) - EXPLORE_CARD_ITEMS);
        const openExploreMore = (title, dimension, rows, year = null) => {
            const primaryTarget = exploreMetricTarget(dimension);
            exploreMoreDialog.value = {
                title: `${title} · All`,
                dimension,
                primary_target: primaryTarget,
                year,
                block_id: null,
                count_only: false,
                rows: exploreRowsByMetric(rows, dimension),
            };
            exploreMoreDialogVisible.value = true;
        };
        const exploreMoreQueryLabel = item => {
            const label = String(item?.label || '');
            if (exploreMoreDialog.value.dimension === 'year_month' && exploreMoreDialog.value.year != null) {
                return `${exploreMoreDialog.value.year}-${label}`;
            }
            return label;
        };
        const openExploreBlockMore = block => {
            const dimension = exploreBlockDimension(block.id);
            const primaryTarget = exploreBlockMetricTarget(block);
            exploreMoreDialog.value = {
                title: `${block.name} · All`,
                dimension,
                primary_target: primaryTarget,
                year: null,
                block_id: block.id,
                count_only: exploreBlockCountOnly(block),
                rows: exploreRowsByMetric(block.rows, dimension),
            };
            exploreMoreDialogVisible.value = true;
        };
        const openExploreMoreStat = (item, target = null) => {
            const detail = exploreMoreDialog.value;
            const resolvedTarget = target || detail.primary_target || 'sets';
            exploreMoreDialogVisible.value = false;
            if (detail.block_id) {
                return openExploreBlockStat(detail.block_id, item?.bucket_id, resolvedTarget);
            }
            return openExploreStat(detail.dimension, item?.value, exploreMoreQueryLabel(item), resolvedTarget);
        };

        const loadExploreBlocks = async () => {
            if (!isAdmin.value || !ExploreApi) {
                exploreBlocks.value = [];
                return;
            }
            try {
                const data = await ExploreApi.blocks();
                exploreBlocks.value = sortExploreBlockItems(data.blocks);
                ensureExploreBlockModes(exploreBlocks.value);
            } catch (error) {
                console.error('Failed to load Explore statistics:', error);
            }
        };

        const loadExploreStats = async () => {
            if (!isAdmin.value || !ExploreApi) return;
            exploreLoading.value = true;
            exploreStatsLoaded.value = false;
            exploreError.value = '';
            try {
                const data = await ExploreApi.stats();
                exploreStats.value = {...emptyExploreStats(), ...data};
                ensureExploreBlockModes(data.custom_blocks);
                const years = (Array.isArray(data.years) ? data.years : [])
                    .map(item => Number(item && item.value))
                    .filter(Number.isFinite)
                    .sort((a, b) => b - a);
                if (!years.includes(Number(exploreSelectedYear.value))) {
                    exploreSelectedYear.value = years.length ? years[0] : null;
                }
                if (data.index) smartAlbumIndex.value = data.index;
                exploreStatsLoaded.value = true;
            } catch (error) {
                exploreStats.value = emptyExploreStats();
                exploreStatsLoaded.value = false;
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                exploreError.value = error.message || 'Explore failed.';
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
            await Promise.all([loadExploreBlocks(), loadExploreStats()]);
            resetExploreBlockModes(exploreBlocks.value);
            await nextTick();
            window.requestAnimationFrame(() => window.scrollTo(0, 0));
        };

        const openCreateExploreBlock = () => {
            exploreBlockEditor.value = {
                id: null,
                name: '',
                description: '',
                display_mode: 'both',
                enabled: true,
                python_code: EXPLORE_BLOCK_DEFAULT_CODE,
            };
            exploreBlockPreview.value = null;
            showExploreBlockDialog.value = true;
        };

        const openEditExploreBlock = async block => {
            let source = exploreBlocks.value.find(item => Number(item.id) === Number(block?.id));
            if (!source) {
                await loadExploreBlocks();
                source = exploreBlocks.value.find(item => Number(item.id) === Number(block?.id));
            }
            if (!source) {
                ElMessage.error('Statistic not found.');
                return;
            }
            exploreBlockEditor.value = {
                id: source.id,
                name: source.name || '',
                description: source.description || '',
                display_mode: normalizeExploreBlockDisplayMode(source),
                enabled: source.enabled !== false,
                python_code: source.python_code || EXPLORE_BLOCK_DEFAULT_CODE,
            };
            exploreBlockPreview.value = null;
            showExploreBlockDialog.value = true;
        };

        const previewExploreBlock = async () => {
            if (!ExploreApi || exploreBlockPreviewLoading.value) return;
            exploreBlockPreviewLoading.value = true;
            exploreBlockPreview.value = null;
            try {
                const data = await ExploreApi.previewBlock({
                    name: exploreBlockEditor.value.name || 'Preview',
                    description: exploreBlockEditor.value.description || '',
                    display_mode: exploreBlockEditor.value.display_mode,
                    python_code: exploreBlockEditor.value.python_code,
                });
                exploreBlockPreview.value = data.card || null;
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                ElMessage.error(error.message || 'Preview failed.');
            } finally {
                exploreBlockPreviewLoading.value = false;
            }
        };

        const exploreBlockPayload = (editor, enabled = editor.enabled !== false) => ({
            name: String(editor.name || '').trim(),
            description: editor.description || '',
            display_mode: ['sets', 'photos', 'both', 'sets_count_only'].includes(editor.display_mode) ? editor.display_mode : 'both',
            enabled,
            python_code: editor.python_code || '',
        });

        const saveExploreBlock = async () => {
            if (!ExploreApi || exploreBlockSaving.value) return;
            const editor = exploreBlockEditor.value;
            const wasExisting = Boolean(editor.id);
            const payload = exploreBlockPayload(editor);
            if (!payload.name) {
                ElMessage.warning('Name required.');
                return;
            }
            exploreBlockSaving.value = true;
            try {
                const data = editor.id
                    ? await ExploreApi.updateBlock(editor.id, payload)
                    : await ExploreApi.createBlock(payload);
                const savedBlock = data?.block || null;
                if (savedBlock?.id) {
                    exploreMetricModes.value[exploreBlockDimension(savedBlock.id)] = exploreBlockDefaultTarget(savedBlock);
                    upsertExploreBlockDefinition(savedBlock);
                    if (savedBlock.enabled === false) {
                        upsertExploreBlockCard(disabledExploreBlockCard(savedBlock));
                    } else {
                        await loadExploreBlockStat(savedBlock.id);
                    }
                }
                showExploreBlockDialog.value = false;
                ElMessage.success(wasExisting ? 'Saved.' : 'Created.');
            } catch (error) {
                ElMessage.error(error.message || 'Save failed.');
            } finally {
                exploreBlockSaving.value = false;
            }
        };

        const toggleExploreBlockEnabled = async () => {
            if (!ExploreApi || exploreBlockSaving.value) return;
            const editor = exploreBlockEditor.value;
            const blockId = Number(editor.id || 0);
            if (!blockId) return;
            const nextEnabled = editor.enabled === false;
            const payload = exploreBlockPayload(editor, nextEnabled);
            if (!payload.name) {
                ElMessage.warning('Name required.');
                return;
            }
            exploreBlockSaving.value = true;
            try {
                const data = await ExploreApi.updateBlock(blockId, payload);
                const savedBlock = data?.block || null;
                editor.enabled = savedBlock?.enabled !== false;
                if (savedBlock?.id) {
                    exploreMetricModes.value[exploreBlockDimension(savedBlock.id)] = exploreBlockDefaultTarget(savedBlock);
                    upsertExploreBlockDefinition(savedBlock);
                    if (savedBlock.enabled === false) {
                        upsertExploreBlockCard(disabledExploreBlockCard(savedBlock));
                        if (Number(exploreMoreDialog.value?.block_id) === Number(savedBlock.id)) {
                            exploreMoreDialogVisible.value = false;
                        }
                    } else {
                        await loadExploreBlockStat(savedBlock.id);
                    }
                }
                ElMessage.success(editor.enabled ? 'Enabled.' : 'Disabled.');
            } catch (error) {
                ElMessage.error(error.message || (nextEnabled ? 'Enable failed.' : 'Disable failed.'));
            } finally {
                exploreBlockSaving.value = false;
            }
        };

        const deleteExploreBlock = async block => {
            const blockId = Number(block?.id || exploreBlockEditor.value.id || 0);
            if (!blockId || !ExploreApi) return;
            const blockName = block?.name || exploreBlockEditor.value.name || '';
            try {
                await ElMessageBox.confirm(
                    `Delete “${blockName}”?`,
                    'Delete Statistic',
                    {confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning'}
                );
                await ExploreApi.deleteBlock(blockId);
                if (Number(exploreBlockEditor.value.id) === blockId) showExploreBlockDialog.value = false;
                removeExploreBlockLocal(blockId);
                ElMessage.success('Deleted.');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || 'Delete failed.');
            }
        };

        const openExploreBlockHelp = async () => {
            showExploreBlockHelpDialog.value = true;
            if (exploreBlockRuntime.value || !ExploreApi) return;
            exploreBlockHelpLoading.value = true;
            try {
                exploreBlockRuntime.value = await ExploreApi.runtime();
            } catch (error) {
                ElMessage.error(error.message || 'Help failed.');
            } finally {
                exploreBlockHelpLoading.value = false;
            }
        };

        const loadSmartHelpers = async () => {
            if (!SmartHelpersApi) return;
            smartHelpersLoading.value = true;
            smartHelpersError.value = '';
            try {
                const data = await SmartHelpersApi.get();
                smartHelpersSource.value = data.source || '';
                smartHelpersFunctions.value = data.helpers || [];
                smartHelpersPath.value = data.path || 'data/custom_helpers.py';
                if (!data.valid) smartHelpersError.value = data.error || 'Validation failed.';
            } catch (error) {
                smartHelpersError.value = error.message || 'Load failed.';
            } finally {
                smartHelpersLoading.value = false;
            }
        };

        const openSmartHelpers = async () => {
            showSmartHelpersDialog.value = true;
            await loadSmartHelpers();
        };

        const validateSmartHelpers = async () => {
            if (!SmartHelpersApi || smartHelpersValidating.value) return false;
            smartHelpersValidating.value = true;
            smartHelpersError.value = '';
            try {
                const data = await SmartHelpersApi.validate(smartHelpersSource.value);
                smartHelpersFunctions.value = data.helpers || [];
                ElMessage.success('Valid.');
                return true;
            } catch (error) {
                smartHelpersError.value = error.message || 'Validation failed.';
                return false;
            } finally {
                smartHelpersValidating.value = false;
            }
        };

        const saveSmartHelpers = async () => {
            if (!SmartHelpersApi || smartHelpersSaving.value) return;
            smartHelpersSaving.value = true;
            smartHelpersError.value = '';
            try {
                const data = await SmartHelpersApi.save(smartHelpersSource.value);
                smartHelpersSource.value = data.source || smartHelpersSource.value;
                smartHelpersFunctions.value = data.helpers || [];
                smartHelpersPath.value = data.path || smartHelpersPath.value;
                // Runtime help is derived from the active helper file, so force all
                // three Smart surfaces to fetch the new contract next time.
                smartAlbumRuntime.value = null;
                smartSetRuntime.value = null;
                exploreBlockRuntime.value = null;
                showSmartHelpersDialog.value = false;
                ElMessage.success('Python Helpers saved.');
            } catch (error) {
                smartHelpersError.value = error.message || 'Save failed.';
            } finally {
                smartHelpersSaving.value = false;
            }
        };

        const exploreBlockPreviewTarget = computed(() => exploreBlockEditor.value.display_mode === 'photos' ? 'photos' : 'sets');
        const exploreBlockPreviewCount = item => exploreBlockPreviewTarget.value === 'photos'
            ? Number(item?.image_count || 0)
            : Number(item?.set_count || 0);
        const exploreBlockPreviewPercentage = item => exploreBlockPreviewTarget.value === 'photos'
            ? Number(item?.image_percentage || 0)
            : Number(item?.set_percentage || 0);
        const exploreBlockPreviewUnit = computed(() => exploreBlockPreviewTarget.value === 'photos' ? 'Photos' : 'Sets');
        const exploreBlockPreviewCountOnly = computed(() => exploreBlockEditor.value.display_mode === 'sets_count_only');

        const showExploreQueryResult = async (data, metadata = {}, fallbackTitle = 'Explore') => {
            if (data.result_type === 'sets') {
                smartSetEntryContext = null;
                currentSmartSet.value = {
                    id: null,
                    name: data.title || fallbackTitle,
                    description: 'Temporary Explore result · Not saved',
                    is_explore: true,
                    explore_target: 'sets',
                    ...metadata,
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
                name: data.title || fallbackTitle,
                description: 'Explore result',
                is_explore: true,
                explore_target: 'photos',
                ...metadata,
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
        };

        const openExploreStat = async (dimension, value, label, target = 'photos') => {
            if (!ExploreApi || exploreQueryLoading.value) return;
            exploreReturnScrollY = window.scrollY || window.pageYOffset || 0;
            exploreQueryLoading.value = true;
            try {
                const data = await ExploreApi.query(dimension, value, label, target);
                await showExploreQueryResult(data, {
                    explore_dimension: dimension,
                    explore_value: value,
                }, `Explore · ${label || ''}`);
            } catch (error) {
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                ElMessage.error(error.message || 'Explore query failed.');
            } finally {
                exploreQueryLoading.value = false;
            }
        };

        const openExploreBlockStat = async (blockId, bucketId, target = 'sets') => {
            if (!ExploreApi || exploreQueryLoading.value || !blockId || !bucketId) return;
            exploreReturnScrollY = window.scrollY || window.pageYOffset || 0;
            exploreQueryLoading.value = true;
            try {
                const data = await ExploreApi.queryBlock(blockId, bucketId, target);
                await showExploreQueryResult(data, {
                    explore_block_id: blockId,
                    explore_bucket_id: bucketId,
                });
            } catch (error) {
                if (error?.payload?.index) smartAlbumIndex.value = error.payload.index;
                ElMessage.error(error.message || 'Explore query failed.');
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
            if (!currentSmartAlbum.value?.id || !SmartAlbumApi) return;
            if (!smartAlbumIndex.value?.ready) {
                smartAlbumImages.value = [];
                smartAlbumQueryError.value = 'Smart View index required. Rebuild it first.';
                return;
            }
            smartAlbumLoading.value = true;
            smartAlbumQueryError.value = '';
            try {
                const data = await SmartAlbumApi.run(currentSmartAlbum.value.id);
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

${trace}` : (error.message || 'Smart Album failed.');
                if (error?.payload?.code === 'smart_album_index_required') {
                    ElMessage.warning(error.message || 'Rebuild the Smart View index first.');
                } else {
                    ElMessage.error(error.message || 'Smart Album failed.');
                }
            } finally {
                smartAlbumLoading.value = false;
            }
        };

        const smartAlbumStepStatusText = (step) => {
            const status = step?.status || 'pending';
            if (status === 'done') return 'Done';
            if (status === 'active') return 'Running';
            if (status === 'error') return 'Failed';
            if (status === 'cancelled') return 'Cancelled';
            return 'Pending';
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
            return list.length ? `${done} / ${list.length} Steps` : 'Ready';
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
                const progress = await SmartAlbumApi.indexProgress();
                smartAlbumIndexProgress.value = progress;
                if (!progress.active) {
                    if (progress.error) throw new Error(progress.error);
                    return progress;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        };

        const refreshSmartAlbumIndex = async (preserveSelection = false) => {
            if (!SmartAlbumApi) return;
            if (smartAlbumSyncActive.value || smartAlbumSyncScanning.value) {
                smartAlbumIndexTab.value = 'sync';
                showSmartAlbumIndexDialog.value = true;
                return;
            }
            smartAlbumIndexTab.value = 'rebuild';
            showSmartAlbumIndexDialog.value = true;
            try {
                await loadLibrarySources();
                const progress = await SmartAlbumApi.indexProgress();
                if (progress.index) smartAlbumIndex.value = progress.index;
                if (!progress.active) {
                    smartAlbumIndexProgress.value = emptySmartAlbumIndexProgress();
                    if (!preserveSelection) initializeSmartAlbumIndexSourceSelection();
                    return;
                }
                smartAlbumIndexProgress.value = progress;
                initializeSmartAlbumIndexSourceSelection(progress);
                if (!smartAlbumIndexRefreshing.value) {
                    smartAlbumIndexRefreshing.value = true;
                    void (async () => {
                        try {
                            const finalProgress = await waitForSmartAlbumIndex();
                            smartAlbumIndex.value = finalProgress.index || await SmartAlbumApi.indexStatus();
                        } catch (error) {
                            ElMessage.error(error.message || 'Index progress failed.');
                        } finally {
                            smartAlbumIndexRefreshing.value = false;
                        }
                    })();
                }
            } catch (error) {
                ElMessage.error(error.message || 'Index status failed.');
            }
        };

        const startSmartAlbumIndexRefresh = async () => {
            if (!SmartAlbumApi || smartAlbumIndexRefreshing.value) return;
            const selectedSourceIds = smartAlbumIndexSelectedSourceIds.value.map(Number);
            if (!selectedSourceIds.length) {
                ElMessage.warning('Select at least one Source.');
                return;
            }
            smartAlbumIndexRefreshing.value = true;
            smartAlbumIndexProgress.value = {
                ...emptySmartAlbumIndexProgress(),
                active: true,
                phase: 'starting',
                message: 'Preparing index rebuild',
                summary: {
                    ...emptySmartAlbumIndexProgress().summary,
                    selected_source_ids: selectedSourceIds,
                    selected_source_names: enabledSmartAlbumIndexSources.value
                        .filter(source => selectedSourceIds.includes(Number(source.id)))
                        .map(source => source.name)
                }
            };
            try {
                const started = await SmartAlbumApi.refreshIndex(selectedSourceIds);
                smartAlbumIndexProgress.value = started;
                const finalProgress = await waitForSmartAlbumIndex();
                const status = finalProgress.index || await SmartAlbumApi.indexStatus();
                smartAlbumIndex.value = status;
                if (finalProgress.phase === 'cancelled') {
                    ElMessage.info('Rebuild cancelled.');
                    return;
                }
                ElMessage.success(`Indexed ${status.asset_count || 0} Photos.`);
                if (currentView.value === 'smart-album' && currentSmartAlbum.value?.id) {
                    await runSmartAlbum();
                }
            } catch (error) {
                smartAlbumIndexProgress.value = {...smartAlbumIndexProgress.value, active: false, phase: 'error', error: error.message || 'Rebuild failed.'};
                ElMessage.error(error.message || 'Rebuild failed.');
            } finally {
                smartAlbumIndexRefreshing.value = false;
            }
        };

        const cancelSmartAlbumIndexRefresh = async () => {
            if (!SmartAlbumApi || !smartAlbumIndexRefreshing.value) return;
            try {
                const progress = await SmartAlbumApi.cancelIndex();
                smartAlbumIndexProgress.value = progress;
            } catch (error) {
                ElMessage.error(error.message || 'Cancel failed.');
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

        const handleSmartAlbumIndexSourceSelectionChange = (ids) => {
            const normalized = [...new Set((Array.isArray(ids) ? ids : []).map(Number).filter(Number.isFinite))];
            smartAlbumIndexSelectedSourceIds.value = normalized;
            smartAlbumSyncSelectedSourceIds.value = [...normalized];
            invalidateSmartAlbumSyncPlan();
        };

        const handleSmartAlbumIndexTabChange = async (name) => {
            if (name === 'sync') {
                if (smartAlbumSyncActive.value || (!smartAlbumSyncPlan.value && smartAlbumSyncProgress.value.phase === 'idle')) {
                    await openSmartAlbumIndexSync(true);
                }
                return;
            }
            await refreshSmartAlbumIndex(true);
        };

        const waitForSmartAlbumSync = async () => {
            while (true) {
                const progress = await SmartAlbumApi.indexSyncProgress();
                smartAlbumSyncProgress.value = progress;
                if (progress.plan) smartAlbumSyncPlan.value = progress.plan;
                if (!progress.active) {
                    if (progress.error) throw new Error(progress.error);
                    return progress;
                }
                await new Promise(resolve => setTimeout(resolve, 500));
            }
        };

        const openSmartAlbumIndexSync = async (preserveSelection = false) => {
            if (!SmartAlbumApi) return;
            if (smartAlbumIndexRefreshing.value) {
                smartAlbumIndexTab.value = 'rebuild';
                showSmartAlbumIndexDialog.value = true;
                return;
            }
            smartAlbumIndexTab.value = 'sync';
            showSmartAlbumIndexDialog.value = true;
            try {
                await loadLibrarySources();
                const progress = await SmartAlbumApi.indexSyncProgress();
                if (progress.index) smartAlbumIndex.value = progress.index;
                if (progress.active) {
                    smartAlbumSyncProgress.value = progress;
                    smartAlbumSyncPlan.value = progress.plan || null;
                    smartAlbumSyncSelectedSourceIds.value = Array.isArray(progress.plan?.source_ids)
                        ? progress.plan.source_ids.map(Number)
                        : [];
                    smartAlbumIndexSelectedSourceIds.value = [...smartAlbumSyncSelectedSourceIds.value];
                    if (!smartAlbumSyncActive.value) {
                        smartAlbumSyncActive.value = true;
                        void (async () => {
                            try {
                                const finalProgress = await waitForSmartAlbumSync();
                                smartAlbumIndex.value = finalProgress.index || await SmartAlbumApi.indexStatus();
                            } catch (error) {
                                ElMessage.error(error.message || 'Sync progress failed.');
                            } finally {
                                smartAlbumSyncActive.value = false;
                            }
                        })();
                    }
                    return;
                }
                smartAlbumSyncProgress.value = emptySmartAlbumSyncProgress();
                smartAlbumSyncPlan.value = null;
                if (!preserveSelection) {
                    initializeSmartAlbumSyncSourceSelection();
                    smartAlbumIndexSelectedSourceIds.value = [...smartAlbumSyncSelectedSourceIds.value];
                } else {
                    smartAlbumSyncSelectedSourceIds.value = [...smartAlbumIndexSelectedSourceIds.value];
                }
            } catch (error) {
                ElMessage.error(error.message || 'Sync status failed.');
            }
        };

        const scanSmartAlbumIndexChanges = async () => {
            if (!SmartAlbumApi || smartAlbumSyncActive.value || smartAlbumSyncScanning.value) return;
            const selectedSourceIds = smartAlbumSyncSelectedSourceIds.value.map(Number);
            if (!selectedSourceIds.length) {
                ElMessage.warning('Select at least one Source.');
                return;
            }
            const unavailable = enabledSmartAlbumIndexSources.value.filter(
                source => selectedSourceIds.includes(Number(source.id)) && !source.available
            );
            if (unavailable.length) {
                ElMessage.warning(`Unavailable: ${unavailable.map(source => source.name).join(', ')}`);
                return;
            }
            smartAlbumSyncScanning.value = true;
            try {
                const plan = await SmartAlbumApi.previewIndexSync(selectedSourceIds);
                smartAlbumSyncPlan.value = plan;
                smartAlbumSyncProgress.value = emptySmartAlbumSyncProgress();
                const summary = plan.summary || {};
                const changes = Number(summary.added_count || 0) + Number(summary.changed_count || 0) + Number(summary.deleted_count || 0);
                if (changes) {
                    ElMessage.success(`Added ${summary.added_count || 0} · Changed ${summary.changed_count || 0} · Deleted ${summary.deleted_count || 0}`);
                } else {
                    ElMessage.success('No changes.');
                }
            } catch (error) {
                smartAlbumSyncPlan.value = null;
                ElMessage.error(error.message || 'Scan failed.');
            } finally {
                smartAlbumSyncScanning.value = false;
            }
        };

        const startSmartAlbumIndexSync = async () => {
            if (!SmartAlbumApi || smartAlbumSyncActive.value || !smartAlbumSyncPlan.value?.plan_id) return;
            const summary = smartAlbumSyncPlan.value.summary || {};
            const changeTotal = Number(summary.added_count || 0) + Number(summary.changed_count || 0) + Number(summary.deleted_count || 0);
            if (!changeTotal) {
                ElMessage.info('No changes.');
                return;
            }
            smartAlbumSyncActive.value = true;
            try {
                const started = await SmartAlbumApi.startIndexSync(smartAlbumSyncPlan.value.plan_id);
                smartAlbumSyncProgress.value = started;
                if (started.plan) smartAlbumSyncPlan.value = started.plan;
                const finalProgress = await waitForSmartAlbumSync();
                const status = finalProgress.index || await SmartAlbumApi.indexStatus();
                smartAlbumIndex.value = status;
                if (finalProgress.phase === 'cancelled') {
                    ElMessage.info('Sync cancelled.');
                    return;
                }
                ElMessage.success(finalProgress.message || 'Sync complete.');
                if (currentView.value === 'smart-album' && currentSmartAlbum.value?.id) {
                    await runSmartAlbum();
                }
            } catch (error) {
                smartAlbumSyncProgress.value = {
                    ...smartAlbumSyncProgress.value,
                    active: false,
                    phase: 'error',
                    error: error.message || 'Sync failed.'
                };
                ElMessage.error(error.message || 'Sync failed.');
            } finally {
                smartAlbumSyncActive.value = false;
            }
        };

        const cancelSmartAlbumIndexSync = async () => {
            if (!SmartAlbumApi || !smartAlbumSyncActive.value) return;
            try {
                smartAlbumSyncProgress.value = await SmartAlbumApi.cancelIndexSync();
            } catch (error) {
                ElMessage.error(error.message || 'Cancel failed.');
            }
        };

        const openSmartAlbumHelp = async () => {
            showSmartAlbumHelpDialog.value = true;
            if (smartAlbumRuntime.value || !SmartAlbumApi) return;
            smartAlbumHelpLoading.value = true;
            try {
                smartAlbumRuntime.value = await SmartAlbumApi.runtime();
            } catch (error) {
                ElMessage.error(error.message || 'Failed to load Smart Album help.');
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
                ElMessage.warning('Name required.');
                return;
            }
            try {
                const data = await SmartAlbumApi.update(smartAlbumEditor.value.id, {
                    name: smartAlbumEditor.value.name,
                    description: smartAlbumEditor.value.description,
                    python_code: smartAlbumEditor.value.python_code
                });
                showEditSmartAlbumDialog.value = false;
                if (data.album) currentSmartAlbum.value = {...currentSmartAlbum.value, ...data.album};
                await loadSmartAlbums();
                if (currentView.value === 'smart-album') await runSmartAlbum();
                ElMessage.success('Smart Album saved.');
            } catch (error) {
                ElMessage.error('Save failed.');
            }
        };

        const deleteSmartAlbum = async (album = currentSmartAlbum.value) => {
            if (!album?.id) return;
            try {
                await ElMessageBox.confirm(
                    `Delete Smart Album “${album.name || ''}”?`,
                    'Delete Smart Album',
                    {confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning'}
                );
                await SmartAlbumApi.remove(album.id);
                ElMessage.success('Deleted.');
                if (currentView.value === 'smart-album') backToAlbums();
                else await loadSmartAlbums();
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') {
                    ElMessage.error(error.message || 'Delete failed.');
                }
            }
        };

        const loadSmartSets = async () => {
            if (!isAdmin.value || !SmartSetApi) {
                smartSets.value = [];
                return;
            }
            try {
                const data = await SmartSetApi.list();
                smartSets.value = Array.isArray(data.sets) ? data.sets : [];
                if (data.index) smartAlbumIndex.value = data.index;
            } catch (error) {
                console.error('Failed to load Smart Set:', error);
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

        const editSmartView = (item) => {
            if (!item) return;
            if (item.smart_view_type === 'set') {
                editSmartSet(item);
                return;
            }
            editSmartAlbum(item);
        };

        const smartViewResultText = (item) => {
            if (!item || item.last_result_count == null) return 'Results: —';
            return item.smart_view_type === 'set'
                ? `Results: ${item.last_result_count} Sets`
                : `Results: ${item.last_result_count} Photos`;
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
                ElMessage.warning('Name required.');
                return;
            }
            const isSet = editor.type === 'set';
            const api = isSet ? SmartSetApi : SmartAlbumApi;
            if (!api) {
                ElMessage.error('Smart View API unavailable.');
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
                ElMessage.success(`${isSet ? 'Smart Set' : 'Smart Album'} created.`);
            } catch (error) {
                ElMessage.error('Create failed.');
            }
        };

        const openSmartViewIndexDialog = async () => {
            if (smartAlbumIndexRefreshing.value) {
                await refreshSmartAlbumIndex(true);
                return;
            }
            if (smartAlbumSyncActive.value || smartAlbumSyncScanning.value) {
                smartAlbumIndexTab.value = 'sync';
                showSmartAlbumIndexDialog.value = true;
                return;
            }
            if (smartAlbumIndexTab.value === 'sync') {
                await openSmartAlbumIndexSync();
                return;
            }
            await refreshSmartAlbumIndex();
        };

        const handleSmartViewCommand = async (command) => {
            if (command === 'index') await openSmartViewIndexDialog();
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
                if (!response.ok) throw new Error(data.error || 'Failed to load Set parent.');
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
            if (!currentSmartSet.value?.id || !SmartSetApi) return;
            smartSetLoading.value = true;
            smartSetQueryError.value = '';
            try {
                const data = await SmartSetApi.run(currentSmartSet.value.id);
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
                smartSetQueryError.value = trace ? `${error.message}\n\n${trace}` : (error.message || 'Run failed.');
                ElMessage.error(error.message || 'Run failed.');
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
                ElMessage.warning('Name required.');
                return;
            }
            try {
                const data = await SmartSetApi.update(smartSetEditor.value.id, {
                    name: smartSetEditor.value.name,
                    description: smartSetEditor.value.description,
                    python_code: smartSetEditor.value.python_code
                });
                showEditSmartSetDialog.value = false;
                if (data.smart_set) currentSmartSet.value = {...currentSmartSet.value, ...data.smart_set};
                await loadSmartSets();
                if (currentView.value === 'smart-set') await runSmartSet();
                ElMessage.success('Smart Set saved.');
            } catch (error) {
                ElMessage.error('Save failed.');
            }
        };

        const deleteSmartSet = async (item = currentSmartSet.value) => {
            if (!item?.id) return;
            try {
                await ElMessageBox.confirm(
                    `Delete Smart Set “${item.name || ''}”? Query only; files are unchanged.`,
                    'Delete Smart Set',
                    {confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning'}
                );
                await SmartSetApi.remove(item.id);
                if (currentView.value === 'smart-set') backToAlbums();
                await loadSmartSets();
                ElMessage.success('Smart Set deleted.');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error(error.message || 'Delete failed.');
            }
        };

        const openSmartSetHelp = async () => {
            showSmartSetHelpDialog.value = true;
            if (smartSetRuntime.value || !SmartSetApi) return;
            smartSetHelpLoading.value = true;
            try {
                smartSetRuntime.value = await SmartSetApi.runtime();
            } catch (error) {
                ElMessage.error(error.message || 'Help unavailable.');
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
                ElMessage.error('Source unavailable.');
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

        // ==================== Feature: Uploaded Albums ====================
        const createAlbum = async () => {
            if (!newAlbum.value.name) {
                ElMessage.warning('Name required.');
                return;
            }

            try {
                const response = await fetch('/api/albums', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(newAlbum.value)
                });

                if (response.ok) {
                    ElMessage.success('Album created.');
                    showCreateAlbumDialog.value = false;
                    resetNewAlbumForm(false);
                    loadAlbums();
                } else {
                    ElMessage.error('Create failed.');
                }
            } catch (error) {
                ElMessage.error('Create failed.');
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

                    ElMessage.success('Saved.');
                    showEditAlbumDialog.value = false;

                    // 重新加载相册列表
                    loadAlbums();

                    // 重新加载当前相册的分组信息
                    await loadAlbumGroupsInfo(currentAlbum.value.id);
                } else {
                    const errorData = await response.json();
                    ElMessage.error(errorData.error || 'Save failed.');
                }
            } catch (error) {
                console.error('Failed to save album:', error);
                ElMessage.error('Save failed.');
            }
        };

        const deleteAlbum = async (albumId) => {
            try {
                await ElMessageBox.confirm('Delete this album and all photos?', 'Delete Album', {
                    confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning'
                });
                const response = await fetch(`/api/albums/${albumId}`, {method: 'DELETE'});
                if (response.ok) {
                    ElMessage.success('Deleted.');
                    backToAlbums();
                    loadAlbums();
                } else {
                    const data = await response.json();
                    ElMessage.error('Delete failed: ' + (data.error || 'Unknown error.'));
                }
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('Delete failed.');
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


        const uploadedAlbumUpload = createUploadedAlbumUpload({
            getAlbumId: () => currentAlbum.value && currentAlbum.value.id,
            loadAlbumImages,
            loadAlbums,
        });
        const handleUploadSuccess = uploadedAlbumUpload.handleUploadSuccess;
        const handleUploadError = uploadedAlbumUpload.handleUploadError;
        const beforeUpload = uploadedAlbumUpload.beforeUpload;

        const deleteImage = async (imageId, fromDetail = false) => {
            try {
                await ElMessageBox.confirm('Delete this photo?', 'Delete Photo', {
                    confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning'
                });

                // 保存当前图片索引，用于详情页删除后的导航
                const currentFilteredIndex = filteredImages.value.findIndex(img => img.id === imageId);

                const response = await fetch(`/api/images/${imageId}`, {method: 'DELETE'});
                if (response.ok) {
                    ElMessage.success('Deleted.');

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
                    ElMessage.error('Delete failed: ' + (data.error || 'Unknown error.'));
                }
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('Delete failed.');
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
                    ElMessage.success('Cover saved.');
                    currentAlbum.value.cover_image_id = imageId;
                    loadAlbums();
                }
            } catch (error) {
                ElMessage.error('Failed to set cover.');
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
                ElMessage.error('Download failed.');
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


        // ==================== Feature: Mapped Library + Workflow composition ====================
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
            smartAlbumSyncSelectedSourceIds.value = [...smartAlbumIndexSelectedSourceIds.value];
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

        const gpsPhotoImport = createGpsPhotoImporter({
            getLocation: () => manifestForm.value && manifestForm.value.location,
            message: ElMessage
        });

        const manifestAutofill = createManifestAutofill({
            getSourceId: () => currentLibrarySource.value && currentLibrarySource.value.id,
            getManifestForm: () => manifestForm.value,
            message: ElMessage
        });

        const workflowTools = createWorkflowTools({
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

        const finalBuilder = createFinalBuilder({
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

        const finalMetadata = createFinalMetadata({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path,
            refreshCurrent: async () => {
                if (currentLibrarySource.value && libraryListing.value && libraryListing.value.path !== null && libraryListing.value.path !== undefined) {
                    await loadLibraryDirectory(libraryListing.value.path || '');
                }
            }
        });

        const setInfo = createSetInfo({
            getSource: () => currentLibrarySource.value,
            getSetPath: () => libraryListing.value && libraryListing.value.path
        });

        const validation = createValidation({
            getSource: () => currentLibrarySource.value
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
            if (shareDirectories.value.length > 0) return 'No photos here. Open a subfolder.';
            if ((Number(stats.unsupported_file_count) || 0) > 0) {
                return `${stats.unsupported_file_count} file${Number(stats.unsupported_file_count) === 1 ? '' : 's'}, no supported photos.`;
            }
            return 'Empty folder.';
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
                return 'No matching Sets.';
            }
            if (allLibraryDirectories.value.length > 0) {
                if (stats.unsupported_file_count > 0) {
                    return `No photos here. ${stats.unsupported_file_count} unsupported file${Number(stats.unsupported_file_count) === 1 ? '' : 's'}.`;
                }
                return 'No photos here. Open a subfolder.';
            }
            if (stats.unsupported_file_count > 0) {
                return `${stats.unsupported_file_count} file${Number(stats.unsupported_file_count) === 1 ? '' : 's'}, no supported photos.`;
            }
            return 'Empty folder.';
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
                ElMessage.warning('New Set is only available at the Set root.');
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
                ElMessage.error('Not a Set root.');
                return;
            }

            const payload = {
                model: String(newSetForm.value.model || '').trim(),
                date: String(newSetForm.value.date || '').trim(),
                theme: String(newSetForm.value.theme || '').trim()
            };
            if (!payload.model || !payload.date || !payload.theme) {
                ElMessage.warning('Model, date, and theme required.');
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
                if (!response.ok) throw new Error(data.error || 'Create failed.');

                showNewSetDialog.value = false;
                await loadLibraryDirectory('');
                librarySelectedDirectoryPath.value = data.path || '';
                ElMessage.success('Set created.');
            } catch (error) {
                ElMessage.error(error?.message || 'Create failed.');
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

            // ==================== Vue template public surface ====================
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
                ElMessage.error('Invalid JSON. Form sync canceled.');
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
                ElMessage.error('Invalid JSON.');
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
                parts.push(`${directoryCount} Folder${directoryCount === 1 ? '' : 's'}`);
            }

            if (fileCount > 0) {
                if (imageCount > 0 && fileCount === imageCount) {
                    parts.push(`${imageCount} Photo${imageCount === 1 ? '' : 's'}`);
                } else if (imageCount > 0) {
                    parts.push(`${fileCount} File${fileCount === 1 ? '' : 's'} (${imageCount} Photo${imageCount === 1 ? '' : 's'})`);
                } else {
                    parts.push(`${fileCount} File${fileCount === 1 ? '' : 's'}`);
                }
            }

            return parts.length > 0 ? parts.join(' · ') : 'Empty';
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
                console.error('Failed to load Library Sources:', error);
            }
        };

        const addLibrarySource = async () => {
            if (!newLibrarySource.value.name.trim() || !newLibrarySource.value.root_path.trim()) {
                ElMessage.warning('Name and path required.');
                return;
            }
            try {
                const response = await fetch('/api/library/sources', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(newLibrarySource.value)
                });
                if (!response.ok) {
                    ElMessage.error('Add failed.');
                    return;
                }
                newLibrarySource.value = {name: 'Completed', root_path: ''};
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
                ElMessage.success('Source added.');
            } catch (error) {
                ElMessage.error('Add failed.');
            }
        };

        const renameLibrarySource = async (source) => {
            try {
                const {value} = await ElMessageBox.prompt('Source name', 'Rename Source', {
                    confirmButtonText: 'Save',
                    cancelButtonText: 'Cancel',
                    inputValue: source.name,
                    inputPattern: /\S+/,
                    inputErrorMessage: 'Name required.'
                });
                const name = String(value || '').trim();
                if (!name || name === source.name) return;

                const response = await fetch(`/api/library/sources/${source.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name})
                });
                if (!response.ok) {
                    ElMessage.error('Rename failed.');
                    return;
                }
                if (currentLibrarySource.value && currentLibrarySource.value.id === source.id) {
                    currentLibrarySource.value = {...currentLibrarySource.value, name};
                }
                await loadLibrarySources();
                ElMessage.success('Source renamed.');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error('Rename failed.');
            }
        };

        const deleteLibrarySource = async (source) => {
            try {
                await ElMessageBox.confirm(`Remove "${source.name}"? Files are not deleted.`, 'Remove Source', {
                    confirmButtonText: 'Remove', cancelButtonText: 'Cancel', type: 'warning'
                });
                const response = await fetch(`/api/library/sources/${source.id}`, {method: 'DELETE'});
                if (!response.ok) {
                    ElMessage.error('Failed to remove password.');
                    return;
                }
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
                ElMessage.success('Source removed.');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error('Failed to remove password.');
            }
        };

        const setLibrarySourceEnabled = async (source, enabled) => {
            const action = enabled ? 'Enable' : 'Disable';
            try {
                if (!enabled) {
                    await ElMessageBox.confirm(
                        `Disable "${source.name}"? It will be excluded from Source features.`,
                        'Disable Source',
                        {confirmButtonText: 'Disable', cancelButtonText: 'Cancel', type: 'warning'}
                    );
                }
                const response = await fetch(`/api/library/sources/${source.id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled})
                });
                if (!response.ok) {
                    ElMessage.error(`${action} failed.`);
                    return;
                }
                manifestAutofill.invalidate();
                manifestKnownValues.value = {};
                await loadLibrarySources();
                ElMessage.success(`Source ${enabled ? 'enabled' : 'disabled'}.`);
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error(`${action} failed.`);
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
                    ElMessage.error(data.error || 'Load failed.');
                    return;
                }
                libraryListing.value = data;
                currentView.value = 'library';
                const params = new URLSearchParams();
                params.set('source', currentLibrarySource.value.id);
                if (data.path) params.set('path', data.path);
                window.history.replaceState({}, '', `${window.location.pathname}?${params.toString()}`);
            } catch (error) {
                ElMessage.error('Load failed.');
            } finally {
                libraryLoading.value = false;
            }
        };

        const openLibrarySource = async (source, path = '') => {
            // A normal Library entry owns its physical parent chain. Smart Set
            // navigation sets a fresh context only after this open succeeds.
            smartSetEntryContext = null;
            if (!source.enabled) {
                ElMessage.warning(`Source disabled: ${source.name}`);
                return;
            }
            if (!source.available) {
                ElMessage.error(`Source unavailable: ${source.root_path}`);
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
            if (!response.ok) throw new Error(data.error || 'Photo info unavailable.');
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
                console.warn('Failed to load photo info:', error);
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
                    ElMessage.error(data.error || 'Action failed.');
                    return;
                }
                image.is_favorited = data.is_favorited;
                const sourceItem = (libraryListing.value.items || []).find(i => i.relative_path === image.relative_path);
                if (sourceItem) sourceItem.is_favorited = data.is_favorited;
                const smartItem = smartAlbumImages.value.find(i => i.id === image.id);
                if (smartItem) smartItem.is_favorited = data.is_favorited;
                ElMessage.success(data.is_favorited ? 'Favorited.' : 'Unfavorited.');
            } catch (error) {
                ElMessage.error('Action failed.');
            }
        };

        const softDeleteCurrentLibraryImage = async () => {
            const image = currentImage.value;
            if (!isAdmin.value || !image || image.source_type !== 'library') return;

            const oldIndex = currentImageIndex.value;
            const filename = image.original_filename || image.name || 'Current photo';
            try {
                await ElMessageBox.confirm(
                    `Delete “${filename}”?`,
                    'Delete Photo',
                    {
                        confirmButtonText: 'Delete',
                        cancelButtonText: 'Cancel',
                        type: 'warning'
                    }
                );

                const response = await fetch(`/api/library/sources/${image.source_id}/soft-delete`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({relative_path: image.relative_path})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Delete failed.');

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

                ElMessage.success('Deleted.');

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
                    ElMessage.error(error?.message || 'Delete failed.');
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
                ElMessage.warning('No favorites.');
                return;
            }
            const text = names.map((name, index) => `${index + 1}. ${name}`).join('\n');

            // 与 uploaded album 的选图交互保持一致：先预览文件名，再由用户确认复制。
            ElMessageBox({
                title: `Folder: ${libraryListing.value.name || currentLibrarySource.value?.name || ''}`,
                message: `Favorites (${names.length}):\n\n${text}`,
                showConfirmButton: true,
                showCancelButton: true,
                confirmButtonText: 'Copy',
                cancelButtonText: 'Close',
                customClass: 'favorite-list-box',
                beforeClose: async (action, instance, done) => {
                    if (action === 'confirm') {
                        try {
                            await navigator.clipboard.writeText(text);
                            ElMessage.success('Copied.');
                            done();
                        } catch (err) {
                            const textArea = document.createElement('textarea');
                            textArea.value = text;
                            document.body.appendChild(textArea);
                            textArea.select();
                            document.execCommand('copy');
                            document.body.removeChild(textArea);
                            ElMessage.success('Copied.');
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
                    ElMessage.error(data.error || 'Rename failed.');
                    return false;
                }
                currentImage.value = data;
                await loadLibraryDirectory(libraryListing.value.path || '');
                currentView.value = 'image-detail';
                ElMessage.success('Renamed.');
                return true;
            } catch (error) {
                ElMessage.error('Rename failed.');
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
                    ElMessage.error(data.error || 'Save failed.');
                    return false;
                }
                currentImage.value.description = data.description || '';
                const sourceItem = (libraryListing.value.items || []).find(i => i.relative_path === image.relative_path);
                if (sourceItem) sourceItem.description = data.description || '';
                const smartItem = smartAlbumImages.value.find(i => i.id === image.id);
                if (smartItem) smartItem.description = data.description || '';
                ElMessage.success('Description saved.');
                return true;
            } catch (error) {
                ElMessage.error('Save failed.');
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
                window.setTimeout(() => void setInfo.loadDetail(), 0);
            }
        };

        const openManifestEditor = async () => {
            if (!isSetDirectory.value) {
                ElMessage.warning('Manifest is only available for Set directories.');
                return;
            }
            const manifest = libraryListing.value.manifest || {};
            if (manifest.exists && !manifest.valid) {
                ElMessage.error('Invalid manifest.json.');
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
                    if (!response.ok) throw new Error(suggested.error || 'Capture time unavailable.');
                    if (!manifestForm.value.shoot.start_time && suggested.shoot?.start_time) {
                        manifestForm.value.shoot.start_time = suggested.shoot.start_time;
                    }
                    if (!manifestForm.value.shoot.end_time && suggested.shoot?.end_time) {
                        manifestForm.value.shoot.end_time = suggested.shoot.end_time;
                    }
                } catch (error) {
                    ElMessage.warning(error?.message || 'EXIF time unavailable.');
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
                    ElMessage.error('Invalid JSON.');
                    return;
                }
                // Raw JSON keeps values/custom fields pass-through; only common
                // storage cleanup and known-field ordering are applied.
                payload = applyManifestCommonStorageRules(payload);
            } else {
                const formPayload = applyManifestStorageRules(trimManifestFormStrings(serializeManifestForm()));
                const currentRaw = parseManifestJsonText();
                if (!currentRaw) {
                    ElMessage.error('Invalid JSON. Custom fields cannot be preserved.');
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
                    ElMessage.error(data.error || 'Save failed.');
                    return;
                }
                showManifestDialog.value = false;
                manifestAutofill.invalidate();
                ElMessage.success('manifest.json saved.');
                await loadLibraryDirectory(libraryListing.value.path || '');
            } catch (error) {
                ElMessage.error('Save failed.');
            }
        };

        const openManifestArray = async () => {
            if (!isLibraryRoot.value || !currentLibrarySource.value) {
                ElMessage.warning('Manifests are only available at the Set root.');
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
                    throw new Error(details ? `${data.error || 'Load failed.'}\n${details}` : (data.error || 'Load failed.'));
                }
                manifestArrayCount.value = Number(data.count) || 0;
                manifestArrayText.value = JSON.stringify(Array.isArray(data.manifests) ? data.manifests : [], null, 2);
            } catch (error) {
                manifestArrayText.value = '';
                ElMessage.error(error?.message || 'Load failed.');
            } finally {
                manifestArrayLoading.value = false;
            }
        };

        const openFolderCompareTool = () => {
            if (!isLibraryRoot.value) {
                ElMessage.warning('Compare Folders is only available at the Set root.');
                return;
            }
            window.open('/folder-compare.html', '_blank', 'noopener');
        };

        const removeLibraryDotfiles = async () => {
            if (!isLibraryRoot.value || !currentLibrarySource.value) {
                ElMessage.warning('Remove Dotfiles is only available at the Set root.');
                return;
            }

            try {
                await ElMessageBox.confirm(
                    'Delete .DS_Store and ._* recursively from this Set root? Other hidden files are unchanged. This cannot be undone.',
                    'Remove Dotfiles',
                    {
                        type: 'warning',
                        confirmButtonText: 'Delete',
                        cancelButtonText: 'Cancel'
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
                if (!response.ok) throw new Error(data.error || 'Remove failed.');

                const removed = Number(data.removed_count) || 0;
                const failed = Number(data.failed_count) || 0;
                if (failed > 0) {
                    ElMessage.warning(`Removed ${removed} dotfiles; ${failed} failed.`);
                } else if (removed > 0) {
                    ElMessage.success(`Removed ${removed} dotfiles.`);
                } else {
                    ElMessage.info('No dotfiles found.');
                }
            } catch (error) {
                ElMessage.error(error?.message || 'Remove failed.');
            }
        };

        const copyManifestArray = async () => {
            if (!manifestArrayText.value) return;
            try {
                await navigator.clipboard.writeText(manifestArrayText.value);
                ElMessage.success(`Copied ${manifestArrayCount.value} manifest${manifestArrayCount.value === 1 ? '' : 's'}.`);
            } catch (error) {
                ElMessage.error('Copy failed.');
            }
        };

        const openLibraryShareDialog = () => {
            if (!isSetDirectory.value) {
                ElMessage.warning('Only full Sets can be shared.');
                return;
            }
            libraryShareForm.value = {
                title: libraryListing.value.name || 'Shared Set',
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
                    ElMessage.error(data.error || 'Share failed.');
                    return;
                }
                showLibraryShareDialog.value = false;
                const shareUrl = `${window.location.origin}${window.location.pathname}?share=${data.token}`;
                await ElMessageBox.confirm(shareUrl, 'Share Link', {
                    confirmButtonText: 'Copy Link', cancelButtonText: 'Close', type: 'success'
                }).then(async () => {
                    await navigator.clipboard.writeText(shareUrl);
                    ElMessage.success('Link copied.');
                }).catch(() => {});
            } catch (error) {
                ElMessage.error('Share failed.');
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
                    libraryShare.value = {title: data.title || 'Shared Set', listing: {items: []}};
                    return;
                }
                if (!response.ok) {
                    ElMessage.error(data.error || 'Share unavailable.');
                    libraryShare.value = {title: 'Share unavailable', listing: {items: []}};
                    return;
                }
                applyLibrarySharePayload(data);
            } catch (error) {
                ElMessage.error('Share unavailable.');
                libraryShare.value = {title: 'Share unavailable', listing: {items: []}};
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
                    ElMessage.error(data.error || 'Load failed.');
                    return;
                }
                applyLibrarySharePayload(data);
                await scrollLibraryPageTop();
            } catch (error) {
                ElMessage.error('Load failed.');
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
                    ElMessage.error(data.error || 'Incorrect password.');
                    return;
                }
                sharePassword.value = '';
                await loadLibraryShare(currentShareToken.value);
            } catch (error) {
                ElMessage.error('Verify failed.');
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
                    ElMessage.error(data.error || 'Selection failed.');
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
                ElMessage.success(data.selected ? 'Selected.' : 'Deselected.');
            } catch (error) {
                ElMessage.error('Selection failed.');
            }
        };

        const exportShareSelection = async () => {
            const names = shareSelectedImages.value.map(item => item.original_filename);
            if (!names.length) {
                ElMessage.warning('No photos selected.');
                return;
            }
            const text = names.map((name, index) => `${index + 1}. ${name}`).join('\n');
            try {
                await navigator.clipboard.writeText(text);
                ElMessage.success(`Copied ${names.length} filename${names.length === 1 ? '' : 's'}.`);
            } catch (error) {
                ElMessageBox.alert(text, 'Selected Files', {confirmButtonText: 'Close'});
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


        // ==================== App lifecycle / shared image controls / admin state ====================
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

                    ElMessage.success('Renamed.');
                } else {
                    const data = await response.json();
                    ElMessage.error(data.error || 'Rename failed.');
                }
            } catch (error) {
                ElMessage.error('Rename failed.');
            }
        };

        // 批量删除图片
        const batchDeleteImages = async () => {
            if (selectedImages.value.length === 0) return;

            try {
                await ElMessageBox.confirm(
                    `Delete ${selectedImages.value.length} selected photos?`,
                    'Warning',
                    {
                        confirmButtonText: 'Delete',
                        cancelButtonText: 'Cancel',
                        type: 'warning',
                    }
                );

                // 逐个删除选中的图片
                const deletePromises = selectedImages.value.map(imageId =>
                    fetch(`/api/images/${imageId}`, {method: 'DELETE'})
                );

                await Promise.all(deletePromises);

                ElMessage.success(`Deleted ${selectedImages.value.length} photos.`);

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
                    ElMessage.error('Delete failed.');
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

                    ElMessage.success(data.is_favorited ? 'Favorited.' : 'Unfavorited.');
                }
            } catch (error) {
                ElMessage.error('Failed.');
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
                ElMessage.warning('Password required.');
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
                    ElMessage.success('Password saved.');
                    newPassword.value = '';
                } else {
                    ElMessage.error('Failed to save password.');
                }
            } catch (error) {
                ElMessage.error('Failed to save password.');
            }
        };

        // 移除相册密码
        const removeAlbumPassword = async () => {
            try {
                const response = await fetch(`/api/albums/${currentAlbum.value.id}/password`, {
                    method: 'DELETE'
                });

                if (response.ok) {
                    ElMessage.success('Password removed.');
                    passwordEnabled.value = false;
                } else {
                    ElMessage.error('Failed to remove password.');
                }
            } catch (error) {
                ElMessage.error('Failed to remove password.');
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
                console.error('Failed to check password status:', error);
            }
        };


        // 显示密码输入对话框
        const showPasswordDialog = (album) => {
            return new Promise((resolve) => {
                ElMessageBox.prompt('Enter the album password.', 'Album Password', {
                    confirmButtonText: 'Unlock',
                    cancelButtonText: 'Cancel',
                    inputType: 'password',

                    inputPlaceholder: 'Password',
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

                                    ElMessage.success('Unlocked.');
                                    done();
                                    resolve(true);
                                } else {
                                    const data = await response.json();
                                    ElMessage.error(data.error || 'Incorrect password.');
                                    instance.inputValue = '';
                                }
                            } catch (error) {
                                ElMessage.error('Verification failed.');
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
                        console.error('Token verification failed:', error);
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
                    title: 'Rename File',
                    message: `
                <div id="rename-form" style="padding: 10px 0;">
                    <p style="margin-bottom: 15px; color: #666;">Current: <strong>${fullFilename}</strong></p>
                    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                        <div style="flex: 1;">
                            <label style="display: block; margin-bottom: 5px; color: #666;">Filename:</label>
                            <input id="name-input" type="text" class="rename-input" 
                                   value="${lastDotIndex > 0 ? fullFilename.substring(0, lastDotIndex) : fullFilename}">
                        </div>
                        <div style="width: 80px;">
                            <label style="display: block; margin-bottom: 5px; color: #666;">Extension:</label>
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
                    confirmButtonText: 'Save',
                    cancelButtonText: 'Cancel',
                    dangerouslyUseHTMLString: true,
                    beforeClose: async (action, instance, done) => {
                        if (action === 'confirm') {
                            const nameInput = document.getElementById('name-input');
                            const extInput = document.getElementById('ext-input');

                            const name = nameInput ? nameInput.value.trim() : '';
                            const ext = extInput ? extInput.value.trim() : '';

                            if (!name) {
                                ElMessage.warning('Filename required.');
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
                    ElMessage.error('Action failed.');
                }
            }
        };

        const editImageDescription = async (image) => {
            try {
                const {value} = await ElMessageBox.prompt('Description', 'Edit Description', {
                    confirmButtonText: 'Save',
                    cancelButtonText: 'Cancel',
                    inputValue: image.description || '',
                    inputPlaceholder: 'Description',
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

                        ElMessage.success('Description saved.');
                    } else {
                        ElMessage.error('Save failed.');
                    }
                }
            } catch (error) {
                if (error !== 'cancel') {
                    ElMessage.error('Action failed.');
                }
            }
        };
        // desc end


        // exif start
        const showExifDialog = ref(false);
        const exifData = ref(null);
        const currentExifImageId = ref(null);

        // EXIF表格数据
        const exifFieldLabels = {
            'Camera Make': 'Camera Make',
            'Camera Model': 'Camera Model',
            'Lens Model': 'Lens Model',
            'Date Taken': 'Date Taken',
            'Focal Length': 'Focal Length',
            'Aperture': 'Aperture',
            'Shutter Speed': 'Shutter Speed',
            'ISO': 'ISO'
        };

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
                                key: exifFieldLabels[fullKey] || fullKey,
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
                    ElMessage.error('EXIF unavailable.');
                }
            } catch (error) {
                ElMessage.error('EXIF unavailable.');
            }
        };
        // exif end


        // title start
        const siteTitle = ref('Photo Library');

        // 加载站点标题
        const loadSiteTitle = async () => {
            try {
                const response = await fetch('/api/albums/title');
                const data = await response.json();
                siteTitle.value = data.title || 'Photo Library';
                // 更新网页标题
                document.title = siteTitle.value;
            } catch (error) {
                console.error('Failed to load title:', error);
            }
        };

        // Edit site title.
        const editSiteTitle = async () => {
            try {
                const {value} = await ElMessageBox.prompt('Site title', 'Edit Site Title', {
                    confirmButtonText: 'Save',
                    cancelButtonText: 'Cancel',
                    inputValue: siteTitle.value,
                    inputPlaceholder: 'Site title',
                    inputValidator: (value) => {
                        if (!value || value.trim() === '') return 'Title required.';
                        if (value.length > 50) return 'Maximum 50 characters.';
                        return true;
                    }
                });

                if (value !== null && value.trim() !== '' && value !== siteTitle.value) {
                    await saveSiteTitle(value.trim());
                }
            } catch (error) {
                if (error !== 'cancel') ElMessage.error('Update failed.');
            }
        };

        const saveSiteTitle = async (newTitle) => {
            try {
                const response = await fetch('/api/albums/title', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: newTitle})
                });

                if (response.ok) {
                    const data = await response.json();
                    siteTitle.value = data.title;
                    document.title = siteTitle.value;
                    ElMessage.success('Title updated.');
                } else {
                    ElMessage.error('Update failed.');
                }
            } catch (error) {
                ElMessage.error('Update failed.');
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
                    ElMessage.warning('No other albums.');
                    return;
                }

                targetAlbumId.value = null;
                showMoveToAlbumDialog.value = true;
            } catch (error) {
                ElMessage.error('Failed to load albums.');
            }
        };

        // 移动选中的图片
        const moveSelectedImages = async () => {
            if (!targetAlbumId.value || selectedImages.value.length === 0) return;

            if (targetAlbumId.value === currentAlbum.value.id) {
                ElMessage.warning('Choose a different album.');
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
                    ElMessage.error(errorData.error || 'Move failed.');
                }
            } catch (error) {
                ElMessage.error('Move failed.');
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
            {label: 'Filename (A–Z)', value: {field: 'original_filename', order: 'asc'}},
            {label: 'Filename (Z–A)', value: {field: 'original_filename', order: 'desc'}},
            {label: 'Uploaded (Newest)', value: {field: 'uploaded_at', order: 'desc'}},
            {label: 'Uploaded (Oldest)', value: {field: 'uploaded_at', order: 'asc'}},

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
            return option ? option.label : 'Sort';
        });

        const changeSmartAlbumSort = (option) => {
            smartAlbumSort.value = {...option.value};
        };

        const getCurrentSmartAlbumSortLabel = computed(() => {
            const option = smartAlbumSortOptions.find(opt =>
                opt.value.field === smartAlbumSort.value.field &&
                opt.value.order === smartAlbumSort.value.order
            );
            return option ? option.label : 'Query Order';
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
                ElMessage.warning('No favorites.');
                return;
            }

            // 构建文件名列表
            const fileNames = favoriteImages.map(img => img.original_filename);
            const fileListText = fileNames.map((name, index) => `${index + 1}. ${name}`).join('\n');

            // 使用 ElMessageBox 显示对话框，带自定义按钮
            ElMessageBox({
                title: `Album: ${currentAlbum.value.name}`,
                message: `Favorites (${favoriteImages.length}):\n\n${fileListText}`,
                showConfirmButton: true,
                showCancelButton: true,
                confirmButtonText: 'Copy',
                cancelButtonText: 'Close',
                customClass: 'favorite-list-box',
                beforeClose: async (action, instance, done) => {
                    if (action === 'confirm') {
                        // 点击复制按钮
                        try {
                            await navigator.clipboard.writeText(fileListText);
                            ElMessage.success('Copied.');
                            done();
                        } catch (err) {
                            // 降级方案：使用老式复制方法
                            const textArea = document.createElement('textarea');
                            textArea.value = fileListText;
                            document.body.appendChild(textArea);
                            textArea.select();
                            document.execCommand('copy');
                            document.body.removeChild(textArea);
                            ElMessage.success('Copied.');
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
                ElMessage.warning('Name required.');
                return;
            }
            try {
                const response = await fetch('/api/album-groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: newGroupName.value.trim(), sort_order: 0})
                });
                if (!response.ok) {
                    ElMessage.error('Create failed.');
                    return;
                }
                newGroupName.value = '';
                await loadAlbums();
                ElMessage.success('Group created.');
            } catch (error) {
                ElMessage.error('Create failed.');
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
                ElMessage.warning('Name required.');
                return;
            }
            try {
                const url = editingGroup.value ? `/api/album-groups/${editingGroup.value.id}` : '/api/album-groups';
                const response = await fetch(url, {
                    method: editingGroup.value ? 'PUT' : 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(groupForm.value)
                });
                if (!response.ok) {
                    ElMessage.error('Save failed.');
                    return;
                }
                const updated = !!editingGroup.value;
                showEditGroupDialog.value = false;
                editingGroup.value = null;
                groupForm.value = {id: null, name: '', sort_order: 0};
                await loadAlbums();
                ElMessage.success(updated ? 'Group updated.' : 'Group created.');
            } catch (error) {
                ElMessage.error('Save failed.');
            }
        };

        const deleteGroup = async (group) => {
            try {
                await ElMessageBox.confirm(
                    `Delete "${group.name}"? Albums will move to Ungrouped.`,
                    'Delete Group',
                    {confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning', distinguishCancelAndClose: true}
                );
                const response = await fetch(`/api/album-groups/${group.id}`, {
                    method: 'DELETE',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({move_to_ungrouped: true})
                });
                if (!response.ok) {
                    ElMessage.error('Delete failed.');
                    return;
                }
                await loadAlbums();
                ElMessage.success('Group deleted.');
            } catch (error) {
                if (error !== 'cancel' && error !== 'close') ElMessage.error('Delete failed.');
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
                console.error('Failed to load album groups:', error);
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
            {label: 'All Photos', value: 'all'},
            {label: 'Favorites', value: 'favorited'},
            {label: 'Not Favorited', value: 'not_favorited'}
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
            return option ? option.label : 'Filter';
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
        const adminToken = ref('');
        const showAdminLogin = ref(false);
        const adminPassword = ref('');
        const adminLoginLoading = ref(false);
        const showChangePasswordDialog = ref(false);
        const newAdminPassword = ref('');
        const passwordLoading = ref(false);

        const verifyAdminPassword = async () => {
            if (!adminPassword.value.trim()) {
                ElMessage.warning('Password required.');
                return;
            }
            adminLoginLoading.value = true;
            try {
                const response = await fetch('/api/admin/verify-password', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({password: adminPassword.value})
                });
                if (!response.ok) {
                    ElMessage.error('Invalid password.');
                    return;
                }
                const data = await response.json();
                isAdmin.value = true;
                adminToken.value = data.token;
                localStorage.setItem('admin_token', data.token);
                showAdminLogin.value = false;
                adminPassword.value = '';
                await loadLibrarySources();
                await loadSmartViews();
                ElMessage.success('Signed in.');
            } catch (error) {
                ElMessage.error('Sign-in failed.');
            } finally {
                adminLoginLoading.value = false;
            }
        };

        const adminLogout = async (notify = true) => {
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
            if (notify) ElMessage.success('Signed out.');
        };

        const changeAdminPassword = async () => {
            const password = newAdminPassword.value.trim();
            if (!password) {
                ElMessage.warning('Password required.');
                return;
            }
            passwordLoading.value = true;
            try {
                const response = await fetch('/api/site-config', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({new_password: password})
                });
                if (!response.ok) {
                    ElMessage.error('Update failed.');
                    return;
                }
                showChangePasswordDialog.value = false;
                newAdminPassword.value = '';
                await adminLogout(false);
                ElMessage.success('Password changed.');
            } catch (error) {
                ElMessage.error('Update failed.');
            } finally {
                passwordLoading.value = false;
            }
        };

        const handleAdminMenuCommand = async (command) => {
            if (command === 'password') {
                newAdminPassword.value = '';
                showChangePasswordDialog.value = true;
                return;
            }
            if (command === 'logout') await adminLogout();
        };

        const restoreAdminStatus = async () => {
            const token = localStorage.getItem('admin_token');
            if (!token) {
                isAdmin.value = false;
                return;
            }
            try {
                const response = await fetch('/api/admin/verify-token', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token})
                });
                if (response.ok) {
                    const data = await response.json();
                    if (data.valid) {
                        adminToken.value = token;
                        isAdmin.value = true;
                        return;
                    }
                }
                localStorage.removeItem('admin_token');
                isAdmin.value = false;
            } catch (error) {
                console.error('Failed to restore admin state:', error);
                isAdmin.value = false;
            }
        };

        const showConfigDialog = ref(false);
        const siteConfig = ref({});
        const configLoading = ref(false);

        const loadSiteConfig = async () => {
            configLoading.value = true;
            try {
                const response = await fetch('/api/site-config');
                if (response.ok) {
                    siteConfig.value = await response.json();
                    showExifOnHover.value = siteConfig.value.show_exif_on_hover === '1';
                }
            } catch (error) {
                console.error('Failed to load site settings:', error);
            } finally {
                configLoading.value = false;
            }
        };

        const saveSiteConfig = async () => {
            configLoading.value = true;
            try {
                const response = await fetch('/api/site-config', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(siteConfig.value)
                });
                if (!response.ok) {
                    ElMessage.error('Save failed.');
                    return;
                }
                showConfigDialog.value = false;
                await loadSiteTitle();
                showExifOnHover.value = siteConfig.value.show_exif_on_hover === '1';
                ElMessage.success('Settings saved.');
            } catch (error) {
                ElMessage.error('Save failed.');
            } finally {
                configLoading.value = false;
            }
        };

        const canUpload = computed(() => {
            if (isAdmin.value) return true;
            return siteConfig.value.allow_guest_upload === '1';
        });

        watch(showConfigDialog, async (newVal) => {
            if (newVal && isAdmin.value) await loadSiteConfig();
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
                title = `Share Album`;
            } else if (type === 'image' && currentImage.value.id && currentAlbum.value.id) {
                shareUrl = generateImageShareUrl(currentAlbum.value.id, currentImage.value.id);
                title = `Share Photo`;
            } else {
                ElMessage.warning('Share link unavailable.');
                return;
            }

            ElMessageBox.confirm(
                `${shareUrl}`,
                {
                    title: title,
                    confirmButtonText: 'Copy Link',
                    cancelButtonText: 'Close',
                    beforeClose: async (action, instance, done) => {
                        if (action === 'confirm') {
                            try {
                                await navigator.clipboard.writeText(shareUrl);
                                ElMessage.success('Copied.');
                                done();
                            } catch (error) {
                                // 降级方案
                                const textArea = document.createElement('textarea');
                                textArea.value = shareUrl;
                                document.body.appendChild(textArea);
                                textArea.select();
                                document.execCommand('copy');
                                document.body.removeChild(textArea);
                                ElMessage.success('Copied.');
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
                    console.error('Failed to load EXIF:', error);
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
                console.error('Failed to load EXIF:', error);
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
                console.error('Failed to load collapse state:', error);
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
            exploreStatsLoaded,
            exploreQueryLoading,
            exploreFullscreenLoading,
            exploreError,
            exploreSourceText,
            exploreSelectedYear,
            exploreYearOptions,
            exploreMonthRows,
            exploreMetricModes,
            exploreMetricTarget,
            exploreMetricCount,
            exploreMetricPercentage,
            exploreMetricUnit,
            exploreBlockHasMetricSelect,
            exploreBlockCountOnly,
            exploreBlockMetricTarget,
            exploreMoreDialogVisible,
            exploreMoreDialog,
            visibleExploreRows,
            exploreHasMore,
            exploreHiddenCount,
            openExploreMore,
            openExploreBlockMore,
            openExploreMoreStat,
            formatExplorePercent,
            openExplore,
            loadExploreStats,
            loadExploreBlocks,
            openExploreStat,
            openExploreBlockStat,
            exploreBlockDimension,
            exploreBlocks,
            exploreBlockDragId,
            exploreBlockDragOverId,
            startExploreBlockDrag,
            overExploreBlockDrag,
            dropExploreBlock,
            clearExploreBlockDrag,
            showExploreBlockDialog,
            exploreBlockSaving,
            exploreBlockPreviewLoading,
            exploreBlockPreview,
            exploreBlockEditor,
            showExploreBlockHelpDialog,
            exploreBlockHelpLoading,
            exploreBlockRuntime,
            showSmartHelpersDialog,
            smartHelpersLoading,
            smartHelpersSaving,
            smartHelpersValidating,
            smartHelpersSource,
            smartHelpersFunctions,
            smartHelpersError,
            smartHelpersPath,
            openSmartHelpers,
            validateSmartHelpers,
            saveSmartHelpers,
            openCreateExploreBlock,
            openEditExploreBlock,
            previewExploreBlock,
            saveExploreBlock,
            toggleExploreBlockEnabled,
            deleteExploreBlock,
            openExploreBlockHelp,
            exploreBlockPreviewCount,
            exploreBlockPreviewPercentage,
            exploreBlockPreviewUnit,
            exploreBlockPreviewCountOnly,
            backToExplore,
            smartViews,
            showCreateSmartViewDialog,
            smartViewCreateEditor,
            openCreateSmartView,
            changeSmartViewCreateType,
            openSmartViewCreateHelp,
            createSmartView,
            openSmartView,
            editSmartView,
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
            enabledSmartAlbumIndexSources,
            smartAlbumIndexedSourceText,
            showSmartAlbumIndexDialog,
            showSmartAlbumIndexHelpDialog,
            smartAlbumIndexTab,
            smartAlbumIndexDialogBusy,
            handleSmartAlbumIndexTabChange,
            handleSmartAlbumIndexSourceSelectionChange,
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
            setStats: setInfo.setStats,
            setStatsLoading: setInfo.statsLoading,
            setStatsError: setInfo.statsError,
            equipmentInfo: setInfo.equipment,
            equipmentState: setInfo.equipmentState,
            equipmentError: setInfo.equipmentError,
            equipmentCameraText: setInfo.cameraText,
            equipmentLensText: setInfo.lensText,
            equipmentFocalText: setInfo.focalText,
            rescanEquipment: setInfo.rescanEquipment,
            validationVisible: validation.validationVisible,
            validationRulesVisible: validation.validationRulesVisible,
            validationLoading: validation.validationLoading,
            validationData: validation.validationData,
            validationError: validation.validationError,
            validationSortMode: validation.validationSortMode,
            validationSearchQuery: validation.validationSearchQuery,
            validationSortedSets: validation.sortedValidationSets,
            validationStatusLabel: validation.validationStatusLabel,
            validationStatusType: validation.validationStatusType,
            validationCountClass: validation.validationCountClass,
            openLibraryValidation: validation.openValidation,
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
            photoImportError: workflowTools.photoImportError,
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
            handleAdminMenuCommand,
            showAdminLogin,
            adminPassword,
            adminLoginLoading,
            showChangePasswordDialog,
            newAdminPassword,
            passwordLoading,
            changeAdminPassword,

            // Site settings
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