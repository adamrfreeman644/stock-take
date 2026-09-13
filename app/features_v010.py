import csv
import io
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, date
from pathlib import Path

from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps


def configure(app, server):
    DB_PATH = server.DB_PATH
    PHOTO_DIR = server.PHOTO_DIR
    BACKUP_DIR = Path(os.environ.get('BACKUP_DIR', '/backups'))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ORIGINAL_DIR = PHOTO_DIR / 'originals'
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)

    def db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def now():
        return datetime.now().isoformat(timespec='seconds')

    def ensure_column(conn, table, name, definition):
        cols = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if name not in cols:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

    def init_schema():
        with db() as conn:
            ensure_column(conn, 'products', 'archived_at', 'TEXT')
            ensure_column(conn, 'photos', 'original_filename', 'TEXT')
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

    def log(conn, action, description, product_id=None, barcode=None, undo_kind=None, undo_payload=None):
        cur = conn.execute('''INSERT INTO activity_log
            (created_at,action,description,product_id,barcode,undo_kind,undo_payload)
            VALUES(?,?,?,?,?,?,?)''', (
            now(), action, description, product_id, barcode, undo_kind,
            json.dumps(undo_payload) if undo_payload is not None else None))
        return cur.lastrowid

    def sync_product(conn, product_id):
        row = conn.execute("SELECT SUM(CASE WHEN state='Available' THEN 1 ELSE 0 END) n FROM item_barcodes WHERE product_id=?", (product_id,)).fetchone()
        qty = int(row['n'] or 0)
        conn.execute("UPDATE products SET quantity=?,status=?,date_sold=? WHERE id=?", (
            qty, 'Available' if qty else 'Sold', None if qty else now(), product_id))

    def current_event(conn):
        manual = conn.execute('SELECT * FROM events WHERE manual_active=1 ORDER BY id DESC LIMIT 1').fetchone()
        if manual:
            return manual
        d = date.today().isoformat()
        return conn.execute('''SELECT * FROM events WHERE manual_active<>-1 AND start_date<=? AND end_date>=?
                              ORDER BY id DESC LIMIT 1''', (d, d)).fetchone()

    def record_sale(conn, barcode_row, source='Manual'):
        product = conn.execute('SELECT * FROM products WHERE id=?', (barcode_row['product_id'],)).fetchone()
        if not product:
            return None
        event = current_event(conn)
        sold_at = now()
        cur = conn.execute('''INSERT INTO sales
            (product_id,barcode_id,barcode,product_name,price_pence,sold_at,source,event_id)
            VALUES(?,?,?,?,?,?,?,?)''', (
            product['id'], barcode_row['id'], barcode_row['barcode'], product['item'], product['price_pence'],
            sold_at, source, event['id'] if event else None))
        sale_id = cur.lastrowid
        conn.execute("UPDATE item_barcodes SET state='Sold',sold_at=? WHERE id=?", (sold_at, barcode_row['id']))
        sync_product(conn, product['id'])
        where = f" at {event['name']}" if event else ''
        log(conn, 'Sale', f"{product['item']} sold{where} via {source}", product['id'], barcode_row['barcode'],
            'undo_sale', {'sale_id': sale_id, 'barcode_id': barcode_row['id'], 'product_id': product['id']})
        return sale_id

    def record_return(conn, barcode_row, source='Manual'):
        sale = conn.execute('SELECT * FROM sales WHERE barcode_id=? AND returned_at IS NULL ORDER BY id DESC LIMIT 1', (barcode_row['id'],)).fetchone()
        if sale:
            conn.execute('UPDATE sales SET returned_at=? WHERE id=?', (now(), sale['id']))
        conn.execute("UPDATE item_barcodes SET state='Available',sold_at=NULL WHERE id=?", (barcode_row['id'],))
        sync_product(conn, barcode_row['product_id'])
        log(conn, 'Restore', f"Barcode {barcode_row['barcode']} restored to stock via {source}", barcode_row['product_id'], barcode_row['barcode'])

    def make_backup(prefix='manual'):
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        target = BACKUP_DIR / f'{prefix}-inventory-{stamp}.db'
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        return target

    def auto_backup_if_due():
        today = date.today().isoformat()
        marker = BACKUP_DIR / '.last-auto-backup'
        try:
            if marker.read_text().strip() == today:
                return
        except OSError:
            pass
        make_backup('auto')
        marker.write_text(today)

    init_schema()

    @app.before_request
    def v010_auto_backup():
        if request.endpoint not in ('static', 'health'):
            try:
                auto_backup_if_due()
            except Exception:
                pass

    def inventory_index():
        view = request.args.get('view', 'cards')
        q = request.args.get('q', '').strip()
        status = request.args.get('status', '').strip()
        with db() as conn:
            if q:
                exact = conn.execute('''SELECT p.id FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                                        WHERE ib.barcode=? AND p.archived_at IS NULL LIMIT 1''', (q,)).fetchone()
                if exact:
                    return redirect(url_for('product_detail', product_id=exact['id']))
            sql = '''SELECT p.*, ph.filename AS main_photo, ph.updated_at AS main_photo_updated,
                     (SELECT MIN(ib.barcode) FROM item_barcodes ib WHERE ib.product_id=p.id) AS first_barcode
                     FROM products p
                     LEFT JOIN photos ph ON ph.id=(SELECT id FROM photos WHERE product_id=p.id ORDER BY sort_order,id LIMIT 1)
                     WHERE p.archived_at IS NULL'''
            params = []
            if q:
                like = f'%{q}%'
                sql += ''' AND (p.item LIKE ? OR p.main_colour LIKE ? OR p.secondary_colour LIKE ? OR p.pattern LIKE ? OR p.notes LIKE ?
                           OR EXISTS(SELECT 1 FROM item_barcodes ib WHERE ib.product_id=p.id AND ib.barcode LIKE ?))'''
                params.extend([like] * 6)
            if status in ('Available', 'Sold'):
                sql += ' AND p.status=?'
                params.append(status)
            sql += ' ORDER BY p.id DESC'
            products = conn.execute(sql, params).fetchall()
            counts = {r['status']: r['c'] for r in conn.execute("SELECT status,COUNT(*) c FROM products WHERE archived_at IS NULL GROUP BY status").fetchall()}
        return render_template('index.html', products=products, view=view, q=q, status=status,
                               available=counts.get('Available', 0), sold=counts.get('Sold', 0))
    app.view_functions['index'] = inventory_index

    def sell_one(product_id):
        with db() as conn:
            b = conn.execute("SELECT * FROM item_barcodes WHERE product_id=? AND state='Available' ORDER BY id LIMIT 1", (product_id,)).fetchone()
            if not b:
                flash('No available item barcodes remain.', 'error')
            else:
                record_sale(conn, b, 'Manual')
                flash('Item sold.', 'success')
        return redirect(url_for('product_detail', product_id=product_id))
    app.view_functions['sell_one'] = sell_one

    def restock_one(product_id):
        with db() as conn:
            b = conn.execute("SELECT * FROM item_barcodes WHERE product_id=? AND state='Sold' ORDER BY id LIMIT 1", (product_id,)).fetchone()
            if not b:
                flash('There is no sold item to restore.', 'info')
            else:
                record_return(conn, b)
        return redirect(url_for('product_detail', product_id=product_id))
    app.view_functions['restock_one'] = restock_one

    def toggle_barcode(barcode_id):
        with db() as conn:
            b = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
            if not b:
                abort(404)
            product_id = b['product_id']
            if b['state'] == 'Available':
                record_sale(conn, b, 'Manual')
            else:
                record_return(conn, b)
        return redirect(url_for('product_detail', product_id=product_id))
    app.view_functions['toggle_item_barcode'] = toggle_barcode

    def toggle_all(product_id):
        with db() as conn:
            rows = conn.execute('SELECT * FROM item_barcodes WHERE product_id=? ORDER BY id', (product_id,)).fetchall()
            available = [r for r in rows if r['state'] == 'Available']
            if available:
                for b in available:
                    record_sale(conn, b, 'Manual')
            else:
                for b in rows:
                    record_return(conn, b)
        return redirect(url_for('product_detail', product_id=product_id))
    app.view_functions['toggle_status'] = toggle_all

    @app.get('/dashboard')
    def dashboard():
        today = date.today().isoformat()
        with db() as conn:
            active = current_event(conn)
            totals = conn.execute('''SELECT COUNT(*) sales, COALESCE(SUM(price_pence),0) revenue
                                     FROM sales WHERE returned_at IS NULL AND substr(sold_at,1,10)=?''', (today,)).fetchone()
            stock = conn.execute("SELECT COALESCE(SUM(quantity),0) qty,COALESCE(SUM(quantity*price_pence),0) value FROM products WHERE archived_at IS NULL").fetchone()
            recent = conn.execute('''SELECT s.*,e.name event_name FROM sales s LEFT JOIN events e ON e.id=s.event_id
                                     ORDER BY s.id DESC LIMIT 12''').fetchall()
            activity = conn.execute('SELECT * FROM activity_log ORDER BY id DESC LIMIT 10').fetchall()
            event_totals = None
            if active:
                event_totals = conn.execute('''SELECT COUNT(*) sales,COALESCE(SUM(price_pence),0) revenue FROM sales
                                               WHERE event_id=? AND returned_at IS NULL''', (active['id'],)).fetchone()
        return render_template('dashboard.html', active_event=active, today_sales=totals, stock=stock,
                               recent_sales=recent, recent_activity=activity, event_totals=event_totals)

    @app.get('/sales')
    def sales_history():
        event_id = request.args.get('event', '').strip()
        with db() as conn:
            sql = 'SELECT s.*,e.name event_name FROM sales s LEFT JOIN events e ON e.id=s.event_id WHERE 1=1'
            params = []
            if event_id.isdigit():
                sql += ' AND s.event_id=?'
                params.append(int(event_id))
            sql += ' ORDER BY s.id DESC'
            sales = conn.execute(sql, params).fetchall()
            events = conn.execute('SELECT * FROM events ORDER BY start_date DESC,id DESC').fetchall()
        return render_template('sales.html', sales=sales, events=events, selected_event=event_id)

    @app.route('/events', methods=['GET', 'POST'])
    def events():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            start = request.form.get('start_date', '').strip()
            end = request.form.get('end_date', '').strip()
            if not name or not start or not end:
                flash('Name, start date and end date are required.', 'error')
            elif end < start:
                flash('End date cannot be before the start date.', 'error')
            else:
                with db() as conn:
                    conn.execute('INSERT INTO events(name,location,start_date,end_date,notes,created_at) VALUES(?,?,?,?,?,?)',
                                 (name, request.form.get('location', '').strip(), start, end, request.form.get('notes', '').strip(), now()))
                    log(conn, 'Event', f"Event created: {name}")
                flash('Event saved.', 'success')
            return redirect(url_for('events'))
        with db() as conn:
            rows = conn.execute('SELECT * FROM events ORDER BY start_date DESC,id DESC').fetchall()
            active = current_event(conn)
        return render_template('events.html', events=rows, active_event=active)

    @app.post('/event/<int:event_id>/start')
    def start_event(event_id):
        with db() as conn:
            conn.execute('UPDATE events SET manual_active=0 WHERE manual_active=1')
            event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if not event:
                abort(404)
            conn.execute('UPDATE events SET manual_active=1 WHERE id=?', (event_id,))
            log(conn, 'Event', f"Started selling event: {event['name']}")
        flash('Selling event started.', 'success')
        return redirect(url_for('events'))

    @app.post('/event/<int:event_id>/end')
    def end_event(event_id):
        with db() as conn:
            event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
            if not event:
                abort(404)
            conn.execute('UPDATE events SET manual_active=-1 WHERE id=?', (event_id,))
            log(conn, 'Event', f"Ended selling event: {event['name']}")
        flash('Selling event ended.', 'success')
        return redirect(url_for('events'))

    @app.post('/event/<int:event_id>/delete')
    def delete_event(event_id):
        with db() as conn:
            used = conn.execute('SELECT COUNT(*) c FROM sales WHERE event_id=?', (event_id,)).fetchone()['c']
            if used:
                flash('This event has sales history and cannot be deleted.', 'error')
            else:
                conn.execute('DELETE FROM events WHERE id=?', (event_id,))
                log(conn, 'Event', f'Event #{event_id} deleted')
        return redirect(url_for('events'))

    @app.get('/activity')
    def activity():
        with db() as conn:
            rows = conn.execute('SELECT * FROM activity_log ORDER BY id DESC LIMIT 300').fetchall()
        return render_template('activity.html', activity=rows)

    @app.post('/activity/<int:log_id>/undo')
    def undo_activity(log_id):
        with db() as conn:
            row = conn.execute('SELECT * FROM activity_log WHERE id=?', (log_id,)).fetchone()
            if not row or not row['undo_kind'] or row['undone_at']:
                flash('That action cannot be undone.', 'error')
                return redirect(request.referrer or url_for('activity'))
            payload = json.loads(row['undo_payload'] or '{}')
            if row['undo_kind'] == 'undo_sale':
                sale = conn.execute('SELECT * FROM sales WHERE id=?', (payload.get('sale_id'),)).fetchone()
                if sale and not sale['returned_at']:
                    conn.execute('UPDATE sales SET returned_at=? WHERE id=?', (now(), sale['id']))
                    conn.execute("UPDATE item_barcodes SET state='Available',sold_at=NULL WHERE id=?", (payload.get('barcode_id'),))
                    sync_product(conn, payload.get('product_id'))
            elif row['undo_kind'] == 'unarchive':
                conn.execute('UPDATE products SET archived_at=NULL WHERE id=?', (payload.get('product_id'),))
            conn.execute('UPDATE activity_log SET undone_at=? WHERE id=?', (now(), log_id))
            log(conn, 'Undo', f"Undid: {row['description']}")
        flash('Last action undone.', 'success')
        return redirect(request.referrer or url_for('activity'))

    @app.post('/product/<int:product_id>/archive')
    def archive_product(product_id):
        with db() as conn:
            p = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            if not p:
                abort(404)
            conn.execute('UPDATE products SET archived_at=? WHERE id=?', (now(), product_id))
            log(conn, 'Archive', f"Archived {p['item']}", product_id, undo_kind='unarchive', undo_payload={'product_id': product_id})
        flash('Product archived.', 'success')
        return redirect(url_for('index'))

    @app.get('/archive')
    def archive():
        with db() as conn:
            products = conn.execute('''SELECT p.*,ph.filename main_photo FROM products p
                LEFT JOIN photos ph ON ph.id=(SELECT id FROM photos WHERE product_id=p.id ORDER BY sort_order,id LIMIT 1)
                WHERE p.archived_at IS NOT NULL ORDER BY p.archived_at DESC''').fetchall()
        return render_template('archive.html', products=products)

    @app.post('/product/<int:product_id>/restore-archive')
    def restore_archive(product_id):
        with db() as conn:
            p = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            if not p:
                abort(404)
            conn.execute('UPDATE products SET archived_at=NULL WHERE id=?', (product_id,))
            log(conn, 'Archive', f"Restored {p['item']}", product_id)
        return redirect(url_for('product_detail', product_id=product_id))

    def permanent_delete(product_id):
        if request.form.get('confirm', '').strip().upper() != 'DELETE':
            flash('Type DELETE in the confirmation box to permanently delete this product.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))
        with db() as conn:
            p = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            if not p:
                abort(404)
            sold = conn.execute('SELECT COUNT(*) c FROM sales WHERE product_id=?', (product_id,)).fetchone()['c']
            if sold:
                flash('This product has sales history. Archive it instead so reports remain accurate.', 'error')
                return redirect(url_for('product_detail', product_id=product_id))
            photos = conn.execute('SELECT filename,original_filename FROM photos WHERE product_id=?', (product_id,)).fetchall()
            conn.execute('DELETE FROM products WHERE id=?', (product_id,))
            log(conn, 'Delete', f"Permanently deleted {p['item']}")
        for ph in photos:
            for name in (ph['filename'], ph['original_filename']):
                if name:
                    try:
                        (PHOTO_DIR / name).unlink(missing_ok=True)
                    except OSError:
                        pass
        return redirect(url_for('index'))
    app.view_functions['delete_product'] = permanent_delete

    @app.get('/backups')
    def backups():
        files = sorted(BACKUP_DIR.glob('*.db'), key=lambda p: p.stat().st_mtime, reverse=True)
        return render_template('backups.html', backups=[{'name': p.name, 'size': p.stat().st_size, 'mtime': datetime.fromtimestamp(p.stat().st_mtime)} for p in files])

    @app.post('/backups/create')
    def backup_create():
        target = make_backup('manual')
        with db() as conn:
            log(conn, 'Backup', f"Backup created: {target.name}")
        flash('Backup created.', 'success')
        return redirect(url_for('backups'))

    @app.get('/backups/download/<path:name>')
    def backup_download(name):
        return send_from_directory(BACKUP_DIR, Path(name).name, as_attachment=True)

    @app.post('/backups/restore/<path:name>')
    def backup_restore(name):
        source_path = BACKUP_DIR / Path(name).name
        if not source_path.exists():
            abort(404)
        make_backup('before-restore')
        source = sqlite3.connect(source_path)
        dest = sqlite3.connect(DB_PATH)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        flash('Backup restored. A safety copy of the previous database was kept.', 'success')
        return redirect(url_for('dashboard'))

    @app.post('/backups/delete/<path:name>')
    def backup_delete(name):
        (BACKUP_DIR / Path(name).name).unlink(missing_ok=True)
        return redirect(url_for('backups'))

    @app.get('/product/<int:product_id>/photos/manage')
    def manage_photos(product_id):
        with db() as conn:
            product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
            photos = conn.execute('SELECT * FROM photos WHERE product_id=? ORDER BY sort_order,id', (product_id,)).fetchall()
        if not product:
            abort(404)
        return render_template('manage_photos.html', product=product, photos=photos)

    @app.get('/photo/<int:photo_id>/download')
    def download_photo(photo_id):
        original = request.args.get('original') == '1'
        with db() as conn:
            ph = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        if not ph:
            abort(404)
        name = ph['original_filename'] if original and ph['original_filename'] else ph['filename']
        return send_from_directory(PHOTO_DIR, name, as_attachment=True)

    @app.post('/photo/<int:photo_id>/main')
    def main_photo(photo_id):
        with db() as conn:
            ph = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            if not ph:
                abort(404)
            others = conn.execute('SELECT id FROM photos WHERE product_id=? AND id<>? ORDER BY sort_order,id', (ph['product_id'], photo_id)).fetchall()
            conn.execute('UPDATE photos SET sort_order=1 WHERE id=?', (photo_id,))
            for i, r in enumerate(others, 2):
                conn.execute('UPDATE photos SET sort_order=? WHERE id=?', (i, r['id']))
            log(conn, 'Photo', f"Main photo changed for product #{ph['product_id']}", ph['product_id'])
            product_id = ph['product_id']
        return redirect(url_for('manage_photos', product_id=product_id))

    @app.post('/photo/<int:photo_id>/crop')
    def crop_photo(photo_id):
        try:
            x = float(request.form.get('x', '0'))
            y = float(request.form.get('y', '0'))
            w = float(request.form.get('w', '1'))
            h = float(request.form.get('h', '1'))
        except ValueError:
            flash('Crop values were invalid.', 'error')
            return redirect(request.referrer or url_for('index'))
        x = max(0, min(1, x)); y = max(0, min(1, y))
        w = max(.02, min(1 - x, w)); h = max(.02, min(1 - y, h))
        with db() as conn:
            ph = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            if not ph:
                abort(404)
            original_name = ph['original_filename']
            if not original_name:
                src = PHOTO_DIR / ph['filename']
                ext = src.suffix.lower() or '.jpg'
                original_name = f"originals/original_{photo_id}_{uuid.uuid4().hex}{ext}"
                (PHOTO_DIR / original_name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, PHOTO_DIR / original_name)
                conn.execute('UPDATE photos SET original_filename=? WHERE id=?', (original_name, photo_id))
            source = PHOTO_DIR / original_name
            target = PHOTO_DIR / ph['filename']
            with Image.open(source) as img:
                iw, ih = img.size
                box = (round(x * iw), round(y * ih), round((x + w) * iw), round((y + h) * ih))
                cropped = img.crop(box)
                if cropped.mode not in ('RGB', 'RGBA') and target.suffix.lower() in ('.jpg', '.jpeg'):
                    cropped = cropped.convert('RGB')
                cropped.save(target)
            conn.execute('UPDATE photos SET updated_at=? WHERE id=?', (datetime.now().isoformat(timespec='microseconds'), photo_id))
            log(conn, 'Photo', f"Photo cropped for product #{ph['product_id']}", ph['product_id'])
            product_id = ph['product_id']
        flash('Crop saved. The original is still available.', 'success')
        return redirect(url_for('manage_photos', product_id=product_id))

    @app.post('/photo/<int:photo_id>/rotate')
    def rotate_photo(photo_id):
        with db() as conn:
            ph = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            if not ph:
                abort(404)
            target = PHOTO_DIR / ph['filename']
            if not target.exists():
                abort(404)
            if not ph['original_filename']:
                ext = target.suffix.lower() or '.jpg'
                original_name = f"originals/original_{photo_id}_{uuid.uuid4().hex}{ext}"
                (PHOTO_DIR / original_name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, PHOTO_DIR / original_name)
                conn.execute('UPDATE photos SET original_filename=? WHERE id=?', (original_name, photo_id))
            with Image.open(target) as image:
                image = ImageOps.exif_transpose(image)
                rotated = image.transpose(Image.Transpose.ROTATE_270)
                if rotated.mode not in ('RGB', 'RGBA') and target.suffix.lower() in ('.jpg', '.jpeg'):
                    rotated = rotated.convert('RGB')
                rotated.save(target)
            conn.execute('UPDATE photos SET updated_at=? WHERE id=?', (datetime.now().isoformat(timespec='microseconds'), photo_id))
            log(conn, 'Photo', f"Photo rotated 90 degrees for product #{ph['product_id']}", ph['product_id'])
            product_id = ph['product_id']
        flash('Photo rotated 90° clockwise. The original is still available.', 'success')
        return redirect(url_for('manage_photos', product_id=product_id))

    @app.post('/photo/<int:photo_id>/reset-crop')
    def reset_crop(photo_id):
        with db() as conn:
            ph = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
            if not ph:
                abort(404)
            if ph['original_filename']:
                shutil.copy2(PHOTO_DIR / ph['original_filename'], PHOTO_DIR / ph['filename'])
                conn.execute('UPDATE photos SET updated_at=? WHERE id=?', (datetime.now().isoformat(timespec='microseconds'), photo_id))
                log(conn, 'Photo', f"Photo edits reset for product #{ph['product_id']}", ph['product_id'])
            product_id = ph['product_id']
        return redirect(url_for('manage_photos', product_id=product_id))

    def paypal_import():
        uploaded = request.files.get('file')
        if not uploaded or not uploaded.filename:
            flash('Choose a PayPal stock CSV first.', 'error')
            return redirect(url_for('paypal'))
        try:
            reader = csv.DictReader(io.StringIO(uploaded.read().decode('utf-8-sig')))
        except Exception as exc:
            flash(f'Could not read CSV: {exc}', 'error')
            return redirect(url_for('paypal'))
        updated = skipped = sales_count = 0
        with db() as conn:
            for row in reader:
                normal = {str(k).strip().lower(): (v or '').strip() for k, v in row.items() if k is not None}
                code = next((normal.get(k) for k in ('barcode', 'sku', 'product code', 'inventory id') if normal.get(k)), '')
                qty_text = next((normal.get(k) for k in ('in stock', 'stock', 'quantity', 'qty') if normal.get(k) != ''), '')
                if not code or qty_text == '':
                    skipped += 1
                    continue
                try:
                    qty = int(float(qty_text))
                except ValueError:
                    skipped += 1
                    continue
                b = conn.execute('SELECT * FROM item_barcodes WHERE barcode=?', (code,)).fetchone()
                if not b:
                    skipped += 1
                    continue
                desired = 'Available' if qty > 0 else 'Sold'
                if b['state'] != desired:
                    if desired == 'Sold':
                        record_sale(conn, b, 'PayPal POS')
                        sales_count += 1
                    else:
                        record_return(conn, b, 'PayPal POS')
                updated += 1
            log(conn, 'PayPal', f"PayPal import completed: {updated} matched, {sales_count} new sales, {skipped} skipped")
        flash(f'PayPal import complete: {updated} matched, {sales_count} new sales, {skipped} skipped.', 'success')
        return redirect(url_for('paypal'))
    app.view_functions['paypal_import'] = paypal_import

    original_add = app.view_functions.get('add_item_barcode')
    if original_add:
        def add_item_barcode_logged(product_id):
            code = request.form.get('barcode', '').strip()
            before = None
            if code:
                with db() as conn:
                    before = conn.execute('SELECT id FROM item_barcodes WHERE barcode=?', (code,)).fetchone()
            response = original_add(product_id)
            if code and not before:
                with db() as conn:
                    b = conn.execute('SELECT id FROM item_barcodes WHERE barcode=? AND product_id=?', (code, product_id)).fetchone()
                    if b:
                        log(conn, 'Barcode', f"Barcode {code} added", product_id, code)
            return response
        app.view_functions['add_item_barcode'] = add_item_barcode_logged

    original_edit_barcode = app.view_functions.get('edit_item_barcode')
    if original_edit_barcode:
        def edit_barcode_logged(barcode_id):
            with db() as conn:
                old = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
            new_code = request.form.get('barcode', '').strip()
            response = original_edit_barcode(barcode_id)
            if old and new_code and old['barcode'] != new_code:
                with db() as conn:
                    current = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
                    if current and current['barcode'] == new_code:
                        log(conn, 'Barcode', f"Barcode changed from {old['barcode']} to {new_code}", old['product_id'], new_code)
            return response
        app.view_functions['edit_item_barcode'] = edit_barcode_logged

    original_new_product = app.view_functions.get('new_product')
    if original_new_product:
        def new_product_logged():
            method = request.method
            response = original_new_product()
            if method == 'POST' and getattr(response, 'status_code', 200) in (301, 302, 303):
                code = request.form.get('barcode', '').strip()
                if code:
                    with db() as conn:
                        row = conn.execute('''SELECT p.id,p.item FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                                            WHERE ib.barcode=? ORDER BY p.id DESC LIMIT 1''', (code,)).fetchone()
                        if row:
                            exists = conn.execute("SELECT 1 FROM activity_log WHERE action='Product' AND product_id=? AND description LIKE 'Created %'", (row['id'],)).fetchone()
                            if not exists:
                                log(conn, 'Product', f"Created {row['item']}", row['id'], code)
            return response
        app.view_functions['new_product'] = new_product_logged

    original_edit_product = app.view_functions.get('edit_product')
    if original_edit_product:
        def edit_product_logged(product_id):
            before = None
            if request.method == 'POST':
                with db() as conn:
                    before = conn.execute('SELECT item,price_pence,main_colour,secondary_colour,pattern,notes FROM products WHERE id=?', (product_id,)).fetchone()
            response = original_edit_product(product_id)
            if before and getattr(response, 'status_code', 200) in (301, 302, 303):
                with db() as conn:
                    after = conn.execute('SELECT item,price_pence,main_colour,secondary_colour,pattern,notes FROM products WHERE id=?', (product_id,)).fetchone()
                    if after and tuple(before) != tuple(after):
                        log(conn, 'Product', f"Edited {after['item']}", product_id)
            return response
        app.view_functions['edit_product'] = edit_product_logged

    return app
