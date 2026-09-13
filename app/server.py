import csv
import io
import os
import sqlite3
import uuid
import urllib.request
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, Response
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', BASE_DIR / 'data'))
PHOTO_DIR = Path(os.environ.get('PHOTO_DIR', BASE_DIR / 'photos'))
UPDATER_DIR = Path(os.environ.get('UPDATER_DIR', BASE_DIR / 'updater-state'))
DB_PATH = DATA_DIR / 'inventory.db'
VERSION = '0.0.8'
LATEST_URL = 'https://raw.githubusercontent.com/adamrfreeman644/raes-bits-and-bobbins-stock/main/VERSION'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif'}
MAX_PHOTOS = 5

for p in (DATA_DIR, PHOTO_DIR, UPDATER_DIR):
    p.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-before-production')
app.config['MAX_CONTENT_LENGTH'] = 40 * 1024 * 1024

try:
    from .photoshoot import bp as photoshoot_bp
    app.register_blueprint(photoshoot_bp)
except ImportError:
    photoshoot_bp = None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db():
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id TEXT UNIQUE,
            item TEXT NOT NULL,
            main_colour TEXT,
            secondary_colour TEXT,
            pattern TEXT,
            price_pence INTEGER NOT NULL DEFAULT 0,
            barcode TEXT UNIQUE,
            quantity INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Available' CHECK(status IN ('Available','Sold')),
            date_added TEXT NOT NULL,
            date_sold TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS item_barcodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            barcode TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'Available' CHECK(state IN ('Available','Sold')),
            added_at TEXT NOT NULL,
            sold_at TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_item_barcodes_product ON item_barcodes(product_id);
        CREATE INDEX IF NOT EXISTS idx_item_barcodes_barcode ON item_barcodes(barcode);
        ''')
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(products)').fetchall()}
        if 'quantity' not in columns:
            conn.execute('ALTER TABLE products ADD COLUMN quantity INTEGER NOT NULL DEFAULT 0')

        # Migrate each legacy product barcode into the per-item barcode table once.
        legacy = conn.execute('SELECT id, COALESCE(NULLIF(barcode,\'\'), NULLIF(inventory_id,\'\')) AS code, status FROM products').fetchall()
        for row in legacy:
            if row['code']:
                exists = conn.execute('SELECT 1 FROM item_barcodes WHERE barcode=?', (row['code'],)).fetchone()
                if not exists:
                    state = 'Sold' if row['status'] == 'Sold' else 'Available'
                    conn.execute('INSERT INTO item_barcodes(product_id,barcode,state,added_at,sold_at) VALUES(?,?,?,?,?)',
                                 (row['id'], row['code'], state, datetime.now().isoformat(timespec='seconds'),
                                  datetime.now().isoformat(timespec='seconds') if state == 'Sold' else None))
        sync_all_products(conn)


def sync_product(conn, product_id):
    row = conn.execute('''SELECT COUNT(*) total,
                                 SUM(CASE WHEN state='Available' THEN 1 ELSE 0 END) available
                          FROM item_barcodes WHERE product_id=?''', (product_id,)).fetchone()
    available = int(row['available'] or 0)
    status = 'Available' if available > 0 else 'Sold'
    date_sold = None if available > 0 else datetime.now().isoformat(timespec='seconds')
    conn.execute('UPDATE products SET quantity=?,status=?,date_sold=? WHERE id=?',
                 (available, status, date_sold, product_id))


def sync_all_products(conn):
    for row in conn.execute('SELECT id FROM products').fetchall():
        sync_product(conn, row['id'])


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_photos(product_id, files, start_order=1):
    saved, order = [], start_order
    for file in files:
        if not file or not file.filename or not allowed_file(file.filename):
            continue
        ext = secure_filename(file.filename).rsplit('.', 1)[1].lower()
        filename = f"p{product_id}_{uuid.uuid4().hex}.{ext}"
        file.save(PHOTO_DIR / filename)
        saved.append((filename, order))
        order += 1
    return saved


def product_with_photos(conn, product_id):
    product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not product:
        return None, [], []
    photos = conn.execute('SELECT * FROM photos WHERE product_id=? ORDER BY sort_order,id', (product_id,)).fetchall()
    barcodes = conn.execute('SELECT * FROM item_barcodes WHERE product_id=? ORDER BY id', (product_id,)).fetchall()
    return product, photos, barcodes


def latest_version():
    try:
        req = urllib.request.Request(LATEST_URL, headers={'User-Agent': 'raes-stock-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read().decode('utf-8').strip()
    except Exception:
        return None


def version_tuple(value):
    try:
        return tuple(int(x) for x in value.strip().lstrip('v').split('.'))
    except Exception:
        return (0,)


def tail(path, limit=80):
    try:
        return '\n'.join(path.read_text(errors='replace').splitlines()[-limit:])
    except OSError:
        return ''


def csv_value(row, *names):
    normalised = {str(k).strip().lower(): (v or '').strip() for k, v in row.items() if k is not None}
    for name in names:
        value = normalised.get(name.lower())
        if value != '':
            return value
    return ''


@app.template_filter('money')
def money(pence):
    return f"£{(pence or 0) / 100:.2f}"


@app.route('/')
def index():
    view = request.args.get('view', 'cards')
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    sql = '''SELECT p.*, ph.filename AS main_photo,
                    (SELECT MIN(ib.barcode) FROM item_barcodes ib WHERE ib.product_id=p.id) AS first_barcode
             FROM products p
             LEFT JOIN photos ph ON ph.id=(SELECT id FROM photos WHERE product_id=p.id ORDER BY sort_order,id LIMIT 1)
             WHERE 1=1'''
    params = []
    if q:
        like = f'%{q}%'
        sql += ''' AND (p.item LIKE ? OR p.main_colour LIKE ? OR p.secondary_colour LIKE ? OR p.pattern LIKE ?
                  OR EXISTS(SELECT 1 FROM item_barcodes ib WHERE ib.product_id=p.id AND ib.barcode LIKE ?))'''
        params.extend([like] * 5)
    if status in ('Available', 'Sold'):
        sql += ' AND p.status=?'
        params.append(status)
    sql += ' ORDER BY p.id DESC'
    with db() as conn:
        products = conn.execute(sql, params).fetchall()
        counts = {r['status']: r['c'] for r in conn.execute('SELECT status,COUNT(*) c FROM products GROUP BY status').fetchall()}
    return render_template('index.html', products=products, view=view, q=q, status=status,
                           available=counts.get('Available', 0), sold=counts.get('Sold', 0))


@app.route('/product/new', methods=['GET', 'POST'])
def new_product():
    if request.method == 'GET':
        return render_template('form.html', product=None, photos=[], duplicate_mode=False)

    barcode = request.form.get('barcode', '').strip()
    item = request.form.get('item', '').strip()
    if not barcode:
        flash('Scan or enter the first physical item barcode.', 'error')
        return render_template('form.html', product=None, photos=[], duplicate_mode=False)
    if not item:
        flash('Item is required.', 'error')
        return render_template('form.html', product=None, photos=[], duplicate_mode=False)
    try:
        price_pence = round(float(request.form.get('price', '0') or 0) * 100)
    except ValueError:
        flash('Price must be a valid number.', 'error')
        return render_template('form.html', product=None, photos=[], duplicate_mode=False)

    files = [request.files.get(f'photo{i}') for i in range(1, MAX_PHOTOS + 1)]
    try:
        with db() as conn:
            cur = conn.execute('''INSERT INTO products
                (inventory_id,item,main_colour,secondary_colour,pattern,price_pence,barcode,quantity,status,date_added,date_sold,notes)
                VALUES (NULL,?,?,?,?,?,NULL,0,'Available',?,NULL,?)''', (
                item, request.form.get('main_colour', '').strip(), request.form.get('secondary_colour', '').strip(),
                request.form.get('pattern', '').strip(), price_pence, datetime.now().isoformat(timespec='seconds'),
                request.form.get('notes', '').strip()))
            product_id = cur.lastrowid
            conn.execute('INSERT INTO item_barcodes(product_id,barcode,state,added_at) VALUES(?,?,\'Available\',?)',
                         (product_id, barcode, datetime.now().isoformat(timespec='seconds')))
            sync_product(conn, product_id)
            for filename, order in save_uploaded_photos(product_id, files):
                conn.execute('INSERT INTO photos(product_id,filename,sort_order) VALUES(?,?,?)', (product_id, filename, order))
    except sqlite3.IntegrityError:
        flash(f'Barcode {barcode} is already assigned to another physical item.', 'error')
        return render_template('form.html', product=None, photos=[], duplicate_mode=False)
    return redirect(url_for('product_detail', product_id=product_id))


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    with db() as conn:
        product, photos, barcodes = product_with_photos(conn, product_id)
    if not product:
        abort(404)
    return render_template('detail.html', product=product, photos=photos, barcodes=barcodes)


@app.route('/product/<int:product_id>/edit', methods=['GET', 'POST'])
def edit_product(product_id):
    with db() as conn:
        product, photos, barcodes = product_with_photos(conn, product_id)
    if not product:
        abort(404)
    if request.method == 'GET':
        return render_template('form.html', product=product, photos=photos, duplicate_mode=False)
    try:
        price_pence = round(float(request.form.get('price', '0') or 0) * 100)
    except ValueError:
        flash('Price must be a valid number.', 'error')
        return render_template('form.html', product=product, photos=photos, duplicate_mode=False)
    with db() as conn:
        conn.execute('''UPDATE products SET item=?,main_colour=?,secondary_colour=?,pattern=?,price_pence=?,notes=? WHERE id=?''', (
            request.form.get('item', '').strip(), request.form.get('main_colour', '').strip(),
            request.form.get('secondary_colour', '').strip(), request.form.get('pattern', '').strip(),
            price_pence, request.form.get('notes', '').strip(), product_id))
        existing = conn.execute('SELECT COUNT(*) c FROM photos WHERE product_id=?', (product_id,)).fetchone()['c']
        files = [request.files.get(f'photo{i}') for i in range(1, MAX_PHOTOS + 1)][:max(0, MAX_PHOTOS - existing)]
        for filename, order in save_uploaded_photos(product_id, files, existing + 1):
            conn.execute('INSERT INTO photos(product_id,filename,sort_order) VALUES(?,?,?)', (product_id, filename, order))
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/product/<int:product_id>/duplicate')
def duplicate_product(product_id):
    with db() as conn:
        product = conn.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
    if not product:
        abort(404)
    return render_template('form.html', product=product, photos=[], duplicate_mode=True)


@app.post('/product/<int:product_id>/barcode/add')
def add_item_barcode(product_id):
    code = request.form.get('barcode', '').strip()
    if not code:
        flash('Scan or enter a barcode.', 'error')
        return redirect(url_for('product_detail', product_id=product_id))
    try:
        with db() as conn:
            if not conn.execute('SELECT 1 FROM products WHERE id=?', (product_id,)).fetchone():
                abort(404)
            conn.execute('INSERT INTO item_barcodes(product_id,barcode,state,added_at) VALUES(?,?,\'Available\',?)',
                         (product_id, code, datetime.now().isoformat(timespec='seconds')))
            sync_product(conn, product_id)
    except sqlite3.IntegrityError:
        flash(f'Barcode {code} is already in use.', 'error')
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/barcode/<int:barcode_id>/edit')
def edit_item_barcode(barcode_id):
    code = request.form.get('barcode', '').strip()
    with db() as conn:
        row = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
        if not row:
            abort(404)
        product_id = row['product_id']
        if not code:
            flash('Barcode cannot be empty.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))
        try:
            conn.execute('UPDATE item_barcodes SET barcode=? WHERE id=?', (code, barcode_id))
        except sqlite3.IntegrityError:
            flash(f'Barcode {code} is already in use.', 'error')
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/barcode/<int:barcode_id>/toggle')
def toggle_item_barcode(barcode_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
        if not row:
            abort(404)
        new_state = 'Sold' if row['state'] == 'Available' else 'Available'
        conn.execute('UPDATE item_barcodes SET state=?,sold_at=? WHERE id=?',
                     (new_state, datetime.now().isoformat(timespec='seconds') if new_state == 'Sold' else None, barcode_id))
        sync_product(conn, row['product_id'])
        product_id = row['product_id']
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/barcode/<int:barcode_id>/delete')
def delete_item_barcode(barcode_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM item_barcodes WHERE id=?', (barcode_id,)).fetchone()
        if not row:
            abort(404)
        product_id = row['product_id']
        conn.execute('DELETE FROM item_barcodes WHERE id=?', (barcode_id,))
        sync_product(conn, product_id)
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/product/<int:product_id>/sell-one')
def sell_one(product_id):
    with db() as conn:
        barcode = conn.execute("SELECT * FROM item_barcodes WHERE product_id=? AND state='Available' ORDER BY id LIMIT 1", (product_id,)).fetchone()
        if not barcode:
            flash('No available item barcodes remain.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))
        conn.execute("UPDATE item_barcodes SET state='Sold',sold_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec='seconds'), barcode['id']))
        sync_product(conn, product_id)
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/product/<int:product_id>/restock-one')
def restock_one(product_id):
    with db() as conn:
        barcode = conn.execute("SELECT * FROM item_barcodes WHERE product_id=? AND state='Sold' ORDER BY id LIMIT 1", (product_id,)).fetchone()
        if not barcode:
            flash('There is no sold barcode to restore. Add a new item barcode instead.', 'info')
            return redirect(url_for('product_detail', product_id=product_id))
        conn.execute("UPDATE item_barcodes SET state='Available',sold_at=NULL WHERE id=?", (barcode['id'],))
        sync_product(conn, product_id)
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/product/<int:product_id>/toggle')
def toggle_status(product_id):
    with db() as conn:
        rows = conn.execute('SELECT * FROM item_barcodes WHERE product_id=?', (product_id,)).fetchall()
        if not rows:
            flash('This product has no item barcodes.', 'error')
            return redirect(url_for('product_detail', product_id=product_id))
        target = 'Sold' if any(r['state'] == 'Available' for r in rows) else 'Available'
        conn.execute('UPDATE item_barcodes SET state=?,sold_at=? WHERE product_id=?',
                     (target, datetime.now().isoformat(timespec='seconds') if target == 'Sold' else None, product_id))
        sync_product(conn, product_id)
    return redirect(url_for('product_detail', product_id=product_id))


@app.post('/product/<int:product_id>/delete')
def delete_product(product_id):
    with db() as conn:
        photos = conn.execute('SELECT filename FROM photos WHERE product_id=?', (product_id,)).fetchall()
        conn.execute('DELETE FROM products WHERE id=?', (product_id,))
    for photo in photos:
        try:
            (PHOTO_DIR / photo['filename']).unlink(missing_ok=True)
        except OSError:
            pass
    return redirect(url_for('index'))


@app.post('/photo/<int:photo_id>/delete')
def delete_photo(photo_id):
    with db() as conn:
        photo = conn.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        if not photo:
            abort(404)
        product_id = photo['product_id']
        conn.execute('DELETE FROM photos WHERE id=?', (photo_id,))
        remaining = conn.execute('SELECT id FROM photos WHERE product_id=? ORDER BY sort_order,id', (product_id,)).fetchall()
        for i, row in enumerate(remaining, 1):
            conn.execute('UPDATE photos SET sort_order=? WHERE id=?', (i, row['id']))
    try:
        (PHOTO_DIR / photo['filename']).unlink(missing_ok=True)
    except OSError:
        pass
    return redirect(url_for('edit_product', product_id=product_id))


THUMBNAIL_SIZES = {
    'inventory': (480, 480),
    'selector': (160, 160),
}


def _safe_photo_path(filename):
    photo_root = Path(PHOTO_DIR).resolve()
    source = (photo_root / filename).resolve()
    try:
        source.relative_to(photo_root)
    except ValueError:
        abort(404)
    if not source.is_file():
        abort(404)
    return photo_root, source


@app.route('/photos/thumbnail/<size>/<path:filename>')
def thumbnail_file(size, filename):
    dimensions = THUMBNAIL_SIZES.get(size)
    if not dimensions:
        abort(404)

    photo_root, source = _safe_photo_path(filename)
    thumbnail_root = photo_root / '.thumbnails' / size
    thumbnail_root.mkdir(parents=True, exist_ok=True)
    # Keep the source extension in the cache name so filenames remain collision-free,
    # while serving every browser thumbnail as a compact, widely supported JPEG.
    thumbnail_name = f"{source.name}.jpg"
    thumbnail = thumbnail_root / thumbnail_name

    try:
        if not thumbnail.exists() or thumbnail.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode != 'RGB':
                    background = Image.new('RGB', image.size, 'white')
                    if image.mode == 'RGBA':
                        background.paste(image, mask=image.getchannel('A'))
                    else:
                        background.paste(image.convert('RGB'))
                    image = background
                image.thumbnail(dimensions, Image.Resampling.LANCZOS)
                temporary = thumbnail.with_suffix('.tmp')
                image.save(temporary, format='JPEG', quality=78, optimize=True, progressive=True)
                temporary.replace(thumbnail)
    except (OSError, ValueError):
        # A corrupt or unsupported source should not break the inventory page.
        return send_from_directory(photo_root, source.relative_to(photo_root))

    response = send_from_directory(thumbnail_root, thumbnail_name, max_age=86400)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@app.route('/photos/<path:filename>')
def photo_file(filename):
    return send_from_directory(PHOTO_DIR, filename)


@app.route('/paypal')
def paypal():
    return render_template('paypal.html')


@app.route('/paypal/export.csv')
def paypal_export():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Price', 'Barcode', 'SKU', 'In Stock'])
    with db() as conn:
        rows = conn.execute('''SELECT p.item,p.price_pence,p.id product_id,ib.barcode,ib.state
                               FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                               ORDER BY p.id,ib.id''').fetchall()
    for row in rows:
        writer.writerow([row['item'], f"{row['price_pence'] / 100:.2f}", row['barcode'], row['barcode'], 1 if row['state'] == 'Available' else 0])
    filename = f"raes-stock-paypal-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.post('/paypal/import')
def paypal_import():
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        flash('Choose a PayPal stock CSV first.', 'error')
        return redirect(url_for('paypal'))
    try:
        text = uploaded.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
    except Exception as exc:
        flash(f'Could not read CSV: {exc}', 'error')
        return redirect(url_for('paypal'))

    updated = 0
    skipped = 0
    touched_products = set()
    with db() as conn:
        for row in reader:
            code = csv_value(row, 'Barcode', 'SKU', 'Product Code', 'Inventory ID')
            qty_text = csv_value(row, 'In Stock', 'Stock', 'Quantity', 'Qty')
            if not code or qty_text == '':
                skipped += 1
                continue
            try:
                qty = int(float(qty_text))
            except ValueError:
                skipped += 1
                continue
            barcode = conn.execute('SELECT * FROM item_barcodes WHERE barcode=?', (code,)).fetchone()
            if not barcode:
                skipped += 1
                continue
            new_state = 'Available' if qty > 0 else 'Sold'
            conn.execute('UPDATE item_barcodes SET state=?,sold_at=? WHERE id=?',
                         (new_state, None if new_state == 'Available' else datetime.now().isoformat(timespec='seconds'), barcode['id']))
            touched_products.add(barcode['product_id'])
            updated += 1
        for product_id in touched_products:
            sync_product(conn, product_id)
    flash(f'PayPal item import complete: {updated} physical barcodes updated, {skipped} skipped.', 'success')
    return redirect(url_for('paypal'))


@app.route('/updates')
def updates():
    latest = latest_version()
    try:
        updater_status = (UPDATER_DIR / 'status').read_text().strip() or 'unknown'
    except OSError:
        updater_status = 'not started'
    return render_template('updates.html', current_version=VERSION, latest_version=latest,
                           update_available=bool(latest and version_tuple(latest) > version_tuple(VERSION)),
                           updater_status=updater_status, update_log=tail(UPDATER_DIR / 'update.log'))


@app.post('/updates/install')
def install_update():
    latest = latest_version()
    if not latest:
        flash('Could not contact GitHub. Update was not requested.', 'error')
        return redirect(url_for('updates'))
    if version_tuple(latest) <= version_tuple(VERSION):
        flash('This installation is already up to date.', 'info')
        return redirect(url_for('updates'))
    try:
        (UPDATER_DIR / 'update.request').write_text(datetime.now().isoformat(timespec='seconds'))
        flash(f'Update to v{latest} requested. The app may briefly restart.', 'success')
    except OSError as exc:
        flash(f'Could not request update: {exc}', 'error')
    return redirect(url_for('updates'))


@app.route('/health')
def health():
    return {'service': 'raes-bits-and-bobbins-stock', 'version': VERSION, 'status': 'ok'}


init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=1975, debug=False)
