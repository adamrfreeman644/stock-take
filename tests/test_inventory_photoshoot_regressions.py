"""Regression checks for inventory filtering and Photo Shoot metadata.

These intentionally cover the small integration bugs that are easy to reintroduce
when changing the mobile inventory or Photo Shoot UI.
"""
from pathlib import Path


def test_primary_inventory_runtime_supports_no_photos_filter():
    source = Path("app/features_v010.py").read_text()
    assert "status == 'No photos'" in source
    assert "NOT EXISTS(SELECT 1 FROM photos np WHERE np.product_id=p.id)" in source


def test_production_wsgi_does_not_install_second_inventory_override():
    source = Path("app/wsgi.py").read_text()
    assert "table_barcode_fix" not in source


def test_inventory_view_menu_keeps_static_view_label_and_all_choices():
    source = Path("app/templates/index.html").read_text()
    assert 'data-view-menu-button' in source
    assert '>View <' in source
    assert 'data-view-choice="table"' in source
    assert 'data-view-choice="cards-{{ n }}"' in source


def test_photo_worker_does_not_assign_from_file_modified_timestamp():
    worker = Path("app/photo_worker.py").read_text()
    assignment_section = worker[worker.index("taken = exif_taken_at(staged)"):]
    assert "datetime.fromtimestamp(float(item['modified_ms'])" not in assignment_section
    assert "No readable camera Date Taken timestamp" in assignment_section
    upload = Path("app/photoshoot.py").read_text()
    auto_section = upload[upload.index("def auto_stage_file()"):upload.index("def auto_finish_upload()")]
    assert "datetime.fromtimestamp(modified_ms" not in auto_section
    assert "no original camera Date Taken metadata; not assigned" in auto_section


def test_photoshoot_assignments_preserve_scanned_barcode():
    source = Path("app/photo_worker.py").read_text()
    assert "source_barcode" in source
    assert "target['inventory_id']" in source


def test_pending_claim_preserves_scanned_barcode():
    source = Path("app/photoshoot.py").read_text()
    assert "INSERT INTO photos(product_id,filename,sort_order,source_barcode)" in source
    assert "(product_id, filename, order, code)" in source


def test_exact_content_hash_duplicate_protection_remains_enabled():
    source = Path("app/photoshoot.py").read_text()
    assert "hashlib.sha256" in source
    assert "content_hash" in source


def test_tenant_schema_has_general_optional_source_barcode():
    source = Path("app/tenant_runtime.py").read_text()
    assert "ensure_column(conn, 'photos', 'source_barcode', 'TEXT')" in source


def test_scanner_has_expiring_single_tab_lease():
    source = Path("app/photoshoot.py").read_text()
    template = Path("app/templates/photoshoot.html").read_text()
    assert "SCANNER_LEASE_SECONDS = 45" in source
    assert "scanner_last_seen" in source
    assert "/scanner/heartbeat" in source
    assert "/scanner/status" in source
    assert "sessionStorage.getItem(scannerKey)" in template
    assert "localStorage.getItem(scannerKey)" not in template
    assert "setInterval" in template and "/photo-shoot/scanner/heartbeat" in template


def test_scanner_does_not_blindly_claim_on_every_page_load():
    source = Path("app/templates/photoshoot.html").read_text()
    load_section = source[source.index("window.addEventListener('load'"):source.index("document.addEventListener('visibilitychange'")]
    assert "/photo-shoot/scanner/status" in load_section
    assert "if(s.owned)showScanner()" in load_section


def test_interrupted_uploads_are_adopted_by_retry_batch():
    source = Path("app/photoshoot.py").read_text()
    assert "duplicate['job_status'] == 'Uploading'" in source
    assert "UPDATE photo_upload_jobs SET batch_token=?" in source
    assert "'recovered': True" in source


def test_upload_ui_lists_individual_photo_failures():
    source = Path("app/templates/photoshoot.html").read_text()
    assert "data-upload-errors" in source
    assert "Photos needing attention" in source
    assert "failures.push" in source


def test_mobile_modals_use_dynamic_viewport_fallback():
    source = Path("app/templates/photoshoot.html").read_text()
    assert "max-height:94vh;max-height:94dvh" in source
    assert "max-height:88vh;max-height:88dvh" in source


def test_heic_backend_support_is_installed():
    requirements = Path("requirements.txt").read_text().lower()
    source = Path("app/photoshoot.py").read_text()
    assert "pillow-heif" in requirements
    assert "register_heif_opener()" in source
    assert "'heic'" in source and "'heif'" in source


def test_barcode_scan_is_in_place_and_keeps_scanner_open():
    template = Path("app/templates/photoshoot.html").read_text()
    backend = Path("app/photoshoot.py").read_text()
    assert "scanForm.addEventListener('submit'" in template
    assert "'X-Requested-With':'XMLHttpRequest'" in template
    assert "renderScan(j)" in template
    assert "wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'" in backend
    assert "'scanned_at': now" in backend
