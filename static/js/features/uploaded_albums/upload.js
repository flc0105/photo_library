const {ElMessage} = window.ElementPlus;

export function createController(options) {
    const handleUploadSuccess = (response, file, fileList) => {
        if (response.immediate_response) {
            // Queue-backed uploads report immediately and finish asynchronously.
            ElMessage.info('Queued.');

            const checkStatus = async (filename) => {
                try {
                    const statusResponse = await fetch(`/api/upload/status/${filename}`);
                    const statusData = await statusResponse.json();

                    if (statusData.status === 'completed') {
                        ElMessage.success(`Processed: ${file.name}`);
                        const albumId = options.getAlbumId && options.getAlbumId();
                        if (albumId !== null && albumId !== undefined) {
                            await options.loadAlbumImages(albumId);
                        }
                        await options.loadAlbums();
                    } else if (statusData.status === 'queued' || statusData.status === 'processing') {
                        window.setTimeout(() => checkStatus(filename), 2000);
                    }
                } catch (error) {
                    console.error('Upload status failed:', error);
                }
            };

            window.setTimeout(() => checkStatus(response.filename), 2000);
        } else {
            ElMessage.success('Uploaded.');
            const albumId = options.getAlbumId && options.getAlbumId();
            if (albumId !== null && albumId !== undefined) {
                void options.loadAlbumImages(albumId);
            }
            void options.loadAlbums();
        }
    };

    const handleUploadError = (error) => {
        try {
            const errorData = JSON.parse(error.message || '{}');
            ElMessage.error(errorData.error || 'Upload failed.');
        } catch (_) {
            ElMessage.error('Upload failed.');
        }
    };

    const beforeUpload = (file) => {
        const isLt100M = file.size / 1024 / 1024 < 100;
        if (!isLt100M) ElMessage.error('Max file size: 100 MB.');
        return isLt100M;
    };

    return {handleUploadSuccess, handleUploadError, beforeUpload};
}
