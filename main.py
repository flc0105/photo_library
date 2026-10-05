from flask import Flask, send_file, send_from_directory
from flask_cors import CORS

from core.auth import load_or_create_session_secret
from core.database import DATABASE, get_db_connection, init_db
from core.operations import create_operations_blueprint
from core.settings import bp as settings_blueprint
import features.mapped_library.browser as mapped_browser
from features.mapped_library.manifest_api import bp as manifest_blueprint
from features.mapped_library.set_info import create_set_info_blueprint
from features.mapped_library.share import bp as share_blueprint
from features.smart.albums import create_smart_album_blueprint
from features.smart.explore import create_explore_blueprint
from features.smart.sets import create_smart_set_blueprint
from features.uploaded_albums.albums import bp as uploaded_albums_blueprint
from features.uploaded_albums.upload import (
    add_md5_to_existing_images,
    bp as upload_blueprint,
    start_upload_worker,
)
from features.workflow.discard_unreturned import create_blueprint as create_discard_unreturned_blueprint
from features.workflow.folder_compare import create_folder_compare_blueprint
from features.workflow.image_inspection import create_blueprint as create_image_inspection_blueprint
from features.workflow.photo_import import create_blueprint as create_photo_import_blueprint
from features.workflow.protect_originals import create_blueprint as create_protect_originals_blueprint
from features.workflow.select_raw import create_blueprint as create_select_raw_blueprint
from features.workflow.sync import create_blueprint as create_sync_blueprint
from features.workflow.validate import create_validate_blueprint
from features.workflow.visual_rename import create_blueprint as create_visual_rename_blueprint
from features.workflow.final.builder import create_final_builder_blueprint
from features.workflow.final.metadata import create_final_metadata_blueprint


app = Flask(__name__)
app.secret_key = load_or_create_session_secret()
CORS(app)


# Frontend delivery remains unchanged in this backend-only refactor.
@app.route('/')
def index():
    return send_file('static/index.html', max_age=0)


@app.route('/<path:path>')
def serve_static(path):
    # send_from_directory performs safe path joining and rejects traversal outside
    # the static directory (for example ../data/photo_library.db or encoded variants).
    return send_from_directory(app.static_folder, path, max_age=0)


# Core / application infrastructure.
app.register_blueprint(settings_blueprint)
app.register_blueprint(create_operations_blueprint(mapped_browser.library_admin_guard))

# Uploaded Album world.
app.register_blueprint(uploaded_albums_blueprint)
app.register_blueprint(upload_blueprint)

# Mapped local library world.
app.register_blueprint(mapped_browser.bp)
app.register_blueprint(manifest_blueprint)
app.register_blueprint(share_blueprint)
app.register_blueprint(create_set_info_blueprint(
    mapped_browser.library_admin_guard,
    mapped_browser.get_library_source,
    mapped_browser.resolve_library_path,
    get_db_connection,
))

# Removable workflow modules. Each module owns one user-facing operation.
_workflow_dependencies = (
    mapped_browser.library_admin_guard,
    mapped_browser.get_library_source,
    mapped_browser.resolve_library_path,
)
app.register_blueprint(create_image_inspection_blueprint(*_workflow_dependencies))
app.register_blueprint(create_visual_rename_blueprint(*_workflow_dependencies, get_db_connection))
app.register_blueprint(create_discard_unreturned_blueprint(*_workflow_dependencies, get_db_connection))
app.register_blueprint(create_protect_originals_blueprint(*_workflow_dependencies))
app.register_blueprint(create_sync_blueprint(*_workflow_dependencies))
app.register_blueprint(create_select_raw_blueprint(*_workflow_dependencies))
app.register_blueprint(create_photo_import_blueprint(*_workflow_dependencies))
app.register_blueprint(create_validate_blueprint(*_workflow_dependencies))
app.register_blueprint(create_final_builder_blueprint(*_workflow_dependencies))
app.register_blueprint(create_final_metadata_blueprint(*_workflow_dependencies))
app.register_blueprint(create_folder_compare_blueprint(mapped_browser.library_admin_guard))

# Smart query surfaces share one index/runtime but remain removable siblings.
app.register_blueprint(create_smart_album_blueprint(mapped_browser.library_admin_guard, DATABASE))
app.register_blueprint(create_smart_set_blueprint(mapped_browser.library_admin_guard, DATABASE))
app.register_blueprint(create_explore_blueprint(mapped_browser.library_admin_guard, DATABASE))


if __name__ == '__main__':
    init_db()
    start_upload_worker()
    # add_md5_to_existing_images()
    app.run(debug=False, host='0.0.0.0', port=8081)
