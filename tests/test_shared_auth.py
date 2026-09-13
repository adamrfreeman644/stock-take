import pytest
import sqlite3
from types import SimpleNamespace

from flask import Flask, session


@pytest.fixture()
def tenant_module(tmp_path, monkeypatch):
    import app.tenant as tenant
    monkeypatch.setattr(tenant, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(tenant, "PHOTO_ROOT", tmp_path / "photos")
    monkeypatch.setattr(tenant, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(tenant, "PLATFORM_DB", tmp_path / "data" / "platform.db")
    for path in (tenant.DATA_DIR, tenant.PHOTO_ROOT, tenant.BACKUP_ROOT, tenant.DATA_DIR / "accounts"):
        path.mkdir(parents=True, exist_ok=True)
    tenant.init_platform()
    return tenant


def test_oidc_subject_becomes_permanent_identity(tenant_module):
    tenant = tenant_module
    with tenant.platform_db() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(email,password_hash,business_name,created_at) VALUES(?,?,?,?)",
            ("owner@example.test", "legacy-password-hash-preserved-for-rollback", "Existing Business", "2026-01-01T00:00:00"),
        )
        legacy_id = cur.lastrowid

    linked = tenant.link_oidc_identity("authentik-sub-123", "owner@example.test", "Owner", auto_provision=False)
    assert linked is not None
    assert linked["id"] == legacy_id
    assert linked["auth_subject"] == "authentik-sub-123"

    # Email can change; permanent tenant resolution remains the immutable OIDC sub.
    again = tenant.link_oidc_identity("authentik-sub-123", "new@example.test", "Owner New", auto_provision=False)
    assert again is not None
    assert again["id"] == legacy_id
    assert again["email"] == "new@example.test"


def test_auto_provisioned_accounts_have_isolated_storage(tenant_module):
    tenant = tenant_module
    a = tenant.link_oidc_identity("sub-a", "a@example.test", "A", auto_provision=True)
    b = tenant.link_oidc_identity("sub-b", "b@example.test", "B", auto_provision=True)
    assert a["id"] != b["id"]
    assert tenant.account_db_path(a["id"]) != tenant.account_db_path(b["id"])
    assert tenant.account_photo_dir(a["id"]) != tenant.account_photo_dir(b["id"])
    assert tenant.account_backup_dir(a["id"]) != tenant.account_backup_dir(b["id"])


def test_oidc_only_account_does_not_store_a_password(tenant_module):
    tenant = tenant_module
    account = tenant.create_oidc_account("sub-no-password", "oidc@example.test", "OIDC Owner")
    row = tenant.get_account(account["id"])
    assert row["password_hash"] == "!oidc-only"
    assert "oidc@example.test" not in row["password_hash"]


def test_duplicate_subject_cannot_map_to_two_tenants(tenant_module):
    tenant = tenant_module
    first = tenant.create_oidc_account("same-sub", "one@example.test", "One")
    second = tenant.link_oidc_identity("same-sub", "two@example.test", "Two", auto_provision=True)
    assert second["id"] == first["id"]
    with tenant.platform_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE auth_subject='same-sub'").fetchone()[0] == 1


def test_auth_enabled_with_missing_oidc_config_is_detected(monkeypatch):
    import app.oidc_auth as oidc
    monkeypatch.setenv("AUTH_ENABLED", "true")
    for name in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    cfg = oidc._cfg()
    assert cfg["enabled"] is True
    assert set(oidc._missing(cfg)) == {"issuer", "client_id", "client_secret", "redirect_uri"}


def test_popup_login_keeps_main_page_available():
    import app.oidc_auth as oidc
    page = oidc._popup_page("/auth/start?next=/dashboard")
    assert "window.open" in page
    assert "inventory-auth-complete" in page


def test_existing_tenant_photos_gain_cache_timestamp_column(tmp_path):
    """Existing account databases must be migrated before photo edits run."""
    from app.tenant_runtime import configure

    database = tmp_path / "inventory.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                item TEXT NOT NULL DEFAULT '',
                price_pence INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT
            );
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 1,
                original_filename TEXT
            );
            CREATE TABLE item_barcodes (
                id INTEGER PRIMARY KEY,
                product_id INTEGER,
                barcode TEXT,
                state TEXT,
                sold_at TEXT
            );
            """
        )

    def open_database():
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        return connection

    app = Flask(__name__)
    app.secret_key = "test"
    server = SimpleNamespace(db=open_database, VERSION="test")
    tenant = SimpleNamespace(
        account_backup_dir=lambda: tmp_path / "backups",
        account_db_path=lambda: database,
    )
    (tmp_path / "backups").mkdir()

    configure(app, server, tenant)
    with app.test_request_context("/"):
        session["account_id"] = 1
        app.extensions["tenant_ensure_feature_schema"]()

    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
    assert "updated_at" in columns
