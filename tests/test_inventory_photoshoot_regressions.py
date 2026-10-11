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
