import shutil
import sqlite3
from datetime import date, datetime

from flask import request, session


def configure(app, server, tenant):
    # Remove the old single-database automatic backup hook. Multi-account backups
    # are handled below inside each owner's private backup directory.
    hooks = app.before_request_funcs.get(None, [])
    app.before_request_funcs[None] = [f for f in hooks if getattr(f, '__name__', '') != 'v010_auto_backup']

    def ensure_column(conn, table, name, definition):
        columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if name not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

    def ensure_feature_schema():
        if not session.get('account_id'):
            return
        with server.db() as conn:
            ensure_column(conn, 'products', 'archived_at', 'TEXT')
            ensure_column(conn, 'photos', 'original_filename', 'TEXT')
            # Photo edits use this value as the browser cache key.  Existing
            # tenant databases pre-date the column, even though the legacy
            # single-account database is migrated by features_v010.init_schema.
            # Without the tenant migration the image file is changed first and
            # the following UPDATE fails, leaving the user with an error page
            # and stale thumbnails.
            ensure_column(conn, 'photos', 'updated_at', 'TEXT')
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                notes TEXT,
                manual_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER,
                barcode_id INTEGER,
                barcode TEXT NOT NULL,
                product_name TEXT NOT NULL,
                price_pence INTEGER NOT NULL,
                sold_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'Manual',
                event_id INTEGER,
                returned_at TEXT,
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL,
                FOREIGN KEY(barcode_id) REFERENCES item_barcodes(id) ON DELETE SET NULL,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
            CREATE INDEX IF NOT EXISTS idx_sales_event ON sales(event_id);
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                description TEXT NOT NULL,
                product_id INTEGER,
                barcode TEXT,
                undo_kind TEXT,
                undo_payload TEXT,
                undone_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            ''')

            # Preserve older sold stock in the sales history for newly-created accounts.
            sold = conn.execute('''SELECT ib.*,p.item,p.price_pence
                                   FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                                   WHERE ib.state='Sold' ''').fetchall()
            for row in sold:
                exists = conn.execute('SELECT 1 FROM sales WHERE barcode_id=? LIMIT 1', (row['id'],)).fetchone()
                if not exists:
                    conn.execute('''INSERT INTO sales(product_id,barcode_id,barcode,product_name,price_pence,sold_at,source,event_id)
                                    VALUES(?,?,?,?,?,?,?,NULL)''',
                                 (row['product_id'], row['id'], row['barcode'], row['item'], row['price_pence'],
                                  row['sold_at'] or datetime.now().isoformat(timespec='seconds'), 'Imported stock'))

    def auto_backup_if_due():
        if not session.get('account_id'):
            return
        directory = tenant.account_backup_dir()
        marker = directory / '.last-auto-backup'
        today = date.today().isoformat()
        try:
            if marker.read_text().strip() == today:
                return
        except OSError:
            pass
        source = tenant.account_db_path()
        if source.exists():
            target = directory / f"auto-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
            shutil.copy2(source, target)
            marker.write_text(today)

    app.extensions['tenant_ensure_feature_schema'] = ensure_feature_schema

    def generic_health():
        return {'service': 'stock-manager', 'version': server.VERSION, 'status': 'ok'}
    app.view_functions['health'] = generic_health

    @app.before_request
    def tenant_feature_guard():
        if not session.get('account_id'):
            return None
        if request.endpoint in ('static', 'health'):
            return None
        ensure_feature_schema()
        try:
            auto_backup_if_due()
        except OSError:
            pass
        return None
