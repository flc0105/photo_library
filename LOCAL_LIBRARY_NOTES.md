# Local Library v3

This revision keeps the original Gallery UI semantics instead of introducing a second gallery UI.

## UI rules

- Folder/Source cards use the existing `album-grid / album-card / album-cover` layout.
- If a folder has a usable cover (`manifest.json -> cover`, or a directly contained supported image), the cover is shown.
- Otherwise the card uses the same Folder icon / "暂无封面" presentation as an empty normal album.
- Images never use `album-card`. They use the existing image layouts:
  - grid: `image-grid / image-item / image-thumb`
  - waterfall: `gallery-waterfall / gallery-item / gallery-img`
- The existing grid/waterfall switch, sort and favorite filters are shared with mapped images.
- Clicking a mapped image enters the existing image-detail screen; detail, rename, EXIF, favorite, download and zoom overlay remain the same interaction.

## Visible files

The mapped browser only returns displayable image extensions (JPG/JPEG/PNG/WebP/GIF/BMP/TIF/TIFF) plus directories. RAW/ARW/CR3/PSD/etc. are not rendered as file rows.

The browse API returns `stats` so the UI distinguishes:

- genuinely empty directory;
- directory with child folders but no direct images;
- directory containing files but no supported displayable images.

## Share behavior

A share is for images directly inside the current directory. If the current directory has no displayable images, the Share button is disabled and the backend also rejects share creation. Navigate into the actual JPG directory first.

Favorite/selection remains one global state per mapped image (`source_id + relative_path`). A model starring an image through a share updates the same favorite state seen by the admin view.


## v3.1 cache fix
- Static `app.js`, `style.css`, and `auth.js` now use a build query string.
- Flask static responses use `max_age=0` to avoid stale HTML/JS mixes during local testing.
- If v3 showed a permanent loading mask immediately after a successful `/browse` 200, clear/reload once after deploying this build.

## v3.2 full-package notes

- Share pages no longer use a separate `el-image` / `share-image-item` UI.
- Once unlocked, a share uses the same `image-grid` / `image-item` and `gallery-waterfall` / `gallery-item` layouts as the original album image view.
- Shared images open the same `image-detail` view, including previous/next navigation, zoom overlay, download, file info, and the same favorite button when selection is allowed.
- Share favorites still write the same global `library_image_states` record used by the administrator's local directory view.
- Shared EXIF hover/detail reads through token-scoped share endpoints; it does not expose the Source API.
- Old share-specific image CSS and `el-image` preview markup were removed.
- Static asset cache version is `20260919-32`.
- This archive is a complete application package, not an incremental patch.
- `DEPLOY_FULL.sh` deploys the complete code while preserving an existing production `gallery.db`.
