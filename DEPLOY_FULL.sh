#!/usr/bin/env bash
set -euo pipefail

# Full-package deploy helper.
# Usage: ./DEPLOY_FULL.sh /path/to/existing/gallery
# It updates the complete application while preserving the target gallery.db.

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 /path/to/existing/gallery" >&2
  exit 2
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$TARGET"

if [[ -f "$TARGET/gallery.db" ]]; then
  cp -a "$TARGET/gallery.db" "$TARGET/gallery.db.pre-v3.2-$(date +%Y%m%d-%H%M%S).bak"
fi

# Copy the whole application tree but keep the production DB if it already exists.
for name in auth_utils.py create_demo_library.py image_utils.py main.py requirements.txt LOCAL_LIBRARY_NOTES.md; do
  cp -a "$SRC/$name" "$TARGET/$name"
done
rm -rf "$TARGET/static"
cp -a "$SRC/static" "$TARGET/static"

if [[ ! -f "$TARGET/gallery.db" ]]; then
  cp -a "$SRC/gallery.db" "$TARGET/gallery.db"
fi

mkdir -p "$TARGET/uploads" "$TARGET/thumbnails" "$TARGET/compressed" "$TARGET/library_cache"

echo "Full application deployed to: $TARGET"
echo "Existing gallery.db was preserved (and backed up when present)."
echo "Restart the Flask process, then hard-refresh the browser once."
