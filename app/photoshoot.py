import json
import os
import shutil
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, redirect, url_for, flash
from PIL import Image, ExifTags
from werkzeug.utils import secure_filename

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('DATA_DIR', BASE_DIR / 'data'))
PHOTO_DIR = Path(os.environ.get('PHOTO_DIR', BASE_DIR / 'photos'))
DB_PATH = DATA_DIR / 'inventory.db'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif'}

bp = Blueprint('photoshoot', __name__, url_prefix='/photo-shoot')


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_tables():
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS photo_shoot_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS photo_shoot_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            scanned_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES photo_shoot_sessions(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS photo_shoot_pending_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            resolved_product_id INTEGER,
            resolved_at TEXT,
            FOREIGN KEY(session_id) REFERENCES photo_shoot_sessions(id),
            FOREIGN KEY(resolved_product_id) REFERENCES products(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pending_scans_session ON photo_shoot_pending_scans(session_id,scanned_at);
        CREATE INDEX IF NOT EXISTS idx_pending_scans_barcode ON photo_shoot_pending_scans(barcode,resolved_product_id);
        CREATE TABLE IF NOT EXISTS photo_upload_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Uploading',
            device_tz_offset INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            queued_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            total_files INTEGER NOT NULL DEFAULT 0,
            uploaded_files INTEGER NOT NULL DEFAULT 0,
            processed_files INTEGER NOT NULL DEFAULT 0,
            assigned_count INTEGER NOT NULL DEFAULT 0,
            unmatched_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            FOREIGN KEY(session_id) REFERENCES photo_shoot_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS photo_upload_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            order_index INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            staged_name TEXT NOT NULL,
            modified_ms REAL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Staged',
            result_json TEXT,
            FOREIGN KEY(job_id) REFERENCES photo_upload_jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS photo_pending_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_scan_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            upload_item_id INTEGER NOT NULL UNIQUE,
            staged_name TEXT NOT NULL,
            original_name TEXT,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            product_id INTEGER,
            FOREIGN KEY(pending_scan_id) REFERENCES photo_shoot_pending_scans(id),
            FOREIGN KEY(job_id) REFERENCES photo_upload_jobs(id),
            FOREIGN KEY(upload_item_id) REFERENCES photo_upload_items(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE INDEX IF NOT EXISTS idx_pending_assets_barcode ON photo_pending_assets(barcode,product_id);
        CREATE INDEX IF NOT EXISTS idx_photo_jobs_status ON photo_upload_jobs(status,id);
        CREATE INDEX IF NOT EXISTS idx_photo_items_job ON photo_upload_items(job_id,order_index,id);
        ''')


def _staging_dir(job_id):
    path = Path(PHOTO_DIR) / '.photo-shoot-staging' / f'job-{int(job_id)}'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_payload(conn, job_id, include_items=False):
    job = conn.execute('SELECT * FROM photo_upload_jobs WHERE id=?', (job_id,)).fetchone()
    if not job:
        return None
    payload = {k: job[k] for k in job.keys()}
    if include_items:
        rows = conn.execute('SELECT * FROM photo_upload_items WHERE job_id=? ORDER BY order_index,id', (job_id,)).fetchall()
        items = []
        for row in rows:
            item = {k: row[k] for k in row.keys()}
            try:
                item['result'] = json.loads(item.pop('result_json') or '{}')
            except (TypeError, ValueError, json.JSONDecodeError):
                item['result'] = {}
            items.append(item)
        payload['items'] = items
    return payload


def _repair_existing_photoshoot_order(conn):
    """Repair already-assigned Photo Shoot images using their saved taken timestamp.

    Only photos created by Photo Shoot jobs are touched. Manual uploads keep their order.
    """
    rows = conn.execute(
        '''SELECT ph.id AS photo_id, ph.product_id, ph.sort_order,
                  i.id AS upload_item_id, i.result_json
           FROM photos ph
           JOIN photo_upload_jobs j
             ON ph.filename LIKE ('p' || ph.product_id || '_shoot' || j.id || '_%')
           JOIN photo_upload_items i
             ON i.job_id=j.id
            AND ph.filename LIKE ('%_shoot' || j.id || '_' || i.id || '.%')
           WHERE i.status='Assigned'
           ORDER BY ph.product_id, ph.id'''
    ).fetchall()

    grouped = defaultdict(list)
    for row in rows:
        taken = None
        try:
            payload = json.loads(row['result_json'] or '{}')
            raw_taken = payload.get('taken')
            if raw_taken:
                taken = datetime.fromisoformat(raw_taken)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        grouped[int(row['product_id'])].append({
            'photo_id': int(row['photo_id']),
            'upload_item_id': int(row['upload_item_id']),
            'taken': taken,
        })

    repaired_products = 0
    repaired_photos = 0
    for product_id, shoot_photos in grouped.items():
        if len(shoot_photos) < 2:
            continue
        shoot_ids = [p['photo_id'] for p in shoot_photos]
        placeholders = ','.join('?' for _ in shoot_ids)
        manual_rows = conn.execute(
            f'''SELECT id FROM photos
                WHERE product_id=? AND id NOT IN ({placeholders})
                ORDER BY sort_order,id''',
            [product_id] + shoot_ids,
        ).fetchall()
        ordered_shoot = sorted(
            shoot_photos,
            key=lambda p: (p['taken'] is None, p['taken'] or datetime.max, p['upload_item_id'], p['photo_id']),
        )
        final_ids = [int(r['id']) for r in manual_rows] + [p['photo_id'] for p in ordered_shoot]
        changed = False
        current_ids = [int(r['id']) for r in conn.execute(
            'SELECT id FROM photos WHERE product_id=? ORDER BY sort_order,id',
            (product_id,),
        ).fetchall()]
        if current_ids != final_ids:
            changed = True
        for order, photo_id in enumerate(final_ids, start=1):
            conn.execute('UPDATE photos SET sort_order=? WHERE id=?', (order, photo_id))
        if changed:
            repaired_products += 1
            repaired_photos += len(shoot_photos)
    return repaired_products, repaired_photos


def claim_pending_photos(product_id, barcode):
    """Attach any Photo Shoot assets waiting for barcode to an existing product."""
    code = str(barcode or '').strip()
    if not code:
        return 0
    init_tables()
    claimed = 0
    with db() as conn:
        assets = conn.execute(
            '''SELECT a.*, i.status AS item_status
               FROM photo_pending_assets a
               JOIN photo_upload_items i ON i.id=a.upload_item_id
               WHERE a.barcode=? AND a.product_id IS NULL
               ORDER BY a.id''',
            (code,),
        ).fetchall()
        for asset in assets:
            staged = _staging_dir(asset['job_id']) / asset['staged_name']
            if not staged.exists():
                continue
            ext = staged.suffix.lower().lstrip('.')
            if ext not in ALLOWED_EXTENSIONS:
                continue
            filename = f"p{int(product_id)}_pending{int(asset['id'])}.{ext}"
            final_path = Path(PHOTO_DIR) / filename
            if not final_path.exists():
                shutil.copy2(staged, final_path)
            exists = conn.execute('SELECT 1 FROM photos WHERE filename=? LIMIT 1', (filename,)).fetchone()
            if not exists:
                order = conn.execute(
                    'SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM photos WHERE product_id=?',
                    (product_id,),
                ).fetchone()['n']
                conn.execute(
                    'INSERT INTO photos(product_id,filename,sort_order) VALUES(?,?,?)',
                    (product_id, filename, order),
                )
            now = datetime.now().isoformat(timespec='seconds')
            conn.execute(
                'UPDATE photo_pending_assets SET product_id=?,claimed_at=? WHERE id=?',
                (product_id, now, asset['id']),
            )
            conn.execute(
                "UPDATE photo_upload_items SET status='Assigned',result_json=? WHERE id=?",
                (json.dumps({'inventory_id': code, 'item': 'Claimed after product creation', 'pending_product': False}, separators=(',', ':')), asset['upload_item_id']),
            )
            conn.execute(
                'UPDATE photo_upload_jobs SET assigned_count=assigned_count+1 WHERE id=?',
                (asset['job_id'],),
            )
            try:
                staged.unlink()
            except OSError:
                pass
            claimed += 1
        if claimed:
            now = datetime.now().isoformat(timespec='seconds')
            conn.execute(
                '''UPDATE photo_shoot_pending_scans
                   SET resolved_product_id=?,resolved_at=?
                   WHERE barcode=? AND resolved_product_id IS NULL''',
                (product_id, now, code),
            )
    return claimed


@bp.after_app_request
def claim_pending_after_barcode_use(response):
    """If any successful POST creates/adds a barcode, immediately claim waiting shoot photos."""
    if request.method == 'POST' and response.status_code < 400:
        code = request.form.get('barcode', '').strip()
        if code:
            try:
                with db() as conn:
                    row = conn.execute(
                        'SELECT product_id FROM item_barcodes WHERE barcode=? LIMIT 1',
                        (code,),
                    ).fetchone()
                if row:
                    claim_pending_photos(int(row['product_id']), code)
            except Exception:
                pass
    return response


@bp.route('/')
def photo_shoot():
    init_tables()
    with db() as conn:
        session = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        scans = []
        current = None
        if session:
            known = conn.execute('''SELECT s.scanned_at,p.item,
                                    (SELECT ib.barcode FROM item_barcodes ib WHERE ib.product_id=p.id ORDER BY ib.id LIMIT 1) AS inventory_id,
                                    0 AS pending
                                    FROM photo_shoot_scans s JOIN products p ON p.id=s.product_id
                                    WHERE s.session_id=?''', (session['id'],)).fetchall()
            waiting = conn.execute('''SELECT scanned_at,'Pending product' AS item,barcode AS inventory_id,1 AS pending
                                      FROM photo_shoot_pending_scans
                                      WHERE session_id=? AND resolved_product_id IS NULL''', (session['id'],)).fetchall()
            scans = sorted([dict(r) for r in known] + [dict(r) for r in waiting], key=lambda r: r['scanned_at'], reverse=True)
            current = scans[0] if scans else None
        recent = conn.execute("SELECT * FROM photo_shoot_sessions ORDER BY id DESC LIMIT 8").fetchall()
        jobs = {}
        for shoot in recent:
            job = conn.execute('SELECT * FROM photo_upload_jobs WHERE session_id=? ORDER BY id DESC LIMIT 1', (shoot['id'],)).fetchone()
            if job:
                jobs[shoot['id']] = {k: job[k] for k in job.keys()}
        pending = conn.execute('''SELECT ps.barcode,MIN(ps.scanned_at) AS first_scanned,
                                 COUNT(DISTINCT ps.id) AS scan_count,
                                 COUNT(DISTINCT pa.id) AS photo_count
                                 FROM photo_shoot_pending_scans ps
                                 LEFT JOIN photo_pending_assets pa ON pa.pending_scan_id=ps.id AND pa.product_id IS NULL
                                 WHERE ps.resolved_product_id IS NULL
                                 GROUP BY ps.barcode
                                 ORDER BY first_scanned DESC''').fetchall()
    return render_template('photoshoot.html', session=session, scans=scans, current=current, recent=recent, jobs=jobs, pending=pending)


@bp.post('/repair-order')
def repair_photo_order():
    init_tables()
    with db() as conn:
        products, photos = _repair_existing_photoshoot_order(conn)
    if products:
        flash(f'Repaired photo order for {products} product(s) / {photos} Photo Shoot image(s). The first photo taken is now primary.', 'success')
    else:
        flash('No reversed Photo Shoot photo groups needed repairing.', 'info')
    return redirect(url_for('photoshoot.photo_shoot'))


@bp.post('/start')
def start_session():
    init_tables()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        conn.execute("UPDATE photo_shoot_sessions SET status='Ended', ended_at=COALESCE(ended_at,?) WHERE status='Active'", (now,))
        conn.execute("INSERT INTO photo_shoot_sessions(started_at,status) VALUES(?, 'Active')", (now,))
    flash('Photo shoot started. Scan the first physical item barcode.', 'success')
    return redirect(url_for('photoshoot.photo_shoot'))


@bp.post('/scan')
def scan_product():
    init_tables()
    code = request.form.get('barcode', '').strip()
    if not code:
        return redirect(url_for('photoshoot.photo_shoot'))
    with db() as conn:
        session = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if not session:
            flash('Start a photo shoot first.', 'error')
            return redirect(url_for('photoshoot.photo_shoot'))
        product = conn.execute('''SELECT p.* FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                                  WHERE ib.barcode=? LIMIT 1''', (code,)).fetchone()
        now = datetime.now().isoformat(timespec='seconds')
        if not product:
            conn.execute(
                'INSERT INTO photo_shoot_pending_scans(session_id,barcode,scanned_at) VALUES(?,?,?)',
                (session['id'], code, now),
            )
            flash(f'Barcode {code} is not in stock yet. Its photo window is being held until that product is created.', 'info')
            return redirect(url_for('photoshoot.photo_shoot'))
        conn.execute('INSERT INTO photo_shoot_scans(session_id,product_id,scanned_at) VALUES(?,?,?)',
                     (session['id'], product['id'], now))
    return redirect(url_for('photoshoot.photo_shoot'))


@bp.post('/end')
def end_session():
    init_tables()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        conn.execute("UPDATE photo_shoot_sessions SET status='Ended', ended_at=? WHERE status='Active'", (now,))
    flash('Photo shoot ended. Select the batch and StockTake will stage it safely before assigning in the background.', 'success')
    return redirect(url_for('photoshoot.photo_shoot'))


@bp.post('/upload/start/<int:session_id>')
def start_batch_upload(session_id):
    init_tables()
    try:
        offset = int(request.form.get('device_tz_offset', '0'))
    except ValueError:
        offset = 0
    with db() as conn:
        shoot = conn.execute('SELECT * FROM photo_shoot_sessions WHERE id=?', (session_id,)).fetchone()
        known = conn.execute('SELECT COUNT(*) AS n FROM photo_shoot_scans WHERE session_id=?', (session_id,)).fetchone()['n']
        pending = conn.execute('SELECT COUNT(*) AS n FROM photo_shoot_pending_scans WHERE session_id=?', (session_id,)).fetchone()['n']
        if not shoot or not (known or pending):
            return jsonify({'ok': False, 'error': 'That shoot has no scan markers.'}), 400
        cur = conn.execute('''INSERT INTO photo_upload_jobs(session_id,status,device_tz_offset,created_at)
                              VALUES(?, 'Uploading', ?, ?)''',
                           (session_id, offset, datetime.now().isoformat(timespec='seconds')))
        job_id = cur.lastrowid
    _staging_dir(job_id)
    return jsonify({'ok': True, 'job_id': job_id})


@bp.post('/upload/file/<int:job_id>')
def stage_batch_file(job_id):
    init_tables()
    uploaded = request.files.get('photo')
    if not uploaded or not uploaded.filename:
        return jsonify({'ok': False, 'error': 'No photo was supplied.'}), 400
    original = secure_filename(uploaded.filename) or f'photo-{uuid.uuid4().hex}'
    if '.' not in original or original.rsplit('.', 1)[1].lower() not in ALLOWED_EXTENSIONS:
        return jsonify({'ok': False, 'error': f'{uploaded.filename}: unsupported image type'}), 400
    try:
        order_index = int(request.form.get('order_index', '0'))
    except ValueError:
        order_index = 0
    try:
        modified_ms = float(request.form.get('modified_ms', '0') or 0)
    except ValueError:
        modified_ms = 0

    with db() as conn:
        job = conn.execute('SELECT * FROM photo_upload_jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['status'] != 'Uploading':
            return jsonify({'ok': False, 'error': 'This upload job is no longer accepting files.'}), 409

    suffix = Path(original).suffix.lower()
    staged_name = f'{order_index:06d}-{uuid.uuid4().hex}{suffix}'
    destination = _staging_dir(job_id) / staged_name
    uploaded.save(destination)
    size_bytes = destination.stat().st_size

    with db() as conn:
        conn.execute('''INSERT INTO photo_upload_items(job_id,order_index,original_name,staged_name,modified_ms,size_bytes,status)
                        VALUES(?,?,?,?,?,?,'Staged')''',
                     (job_id, order_index, original, staged_name, modified_ms, size_bytes))
        conn.execute('UPDATE photo_upload_jobs SET uploaded_files=uploaded_files+1 WHERE id=?', (job_id,))
        count = conn.execute('SELECT uploaded_files FROM photo_upload_jobs WHERE id=?', (job_id,)).fetchone()['uploaded_files']
    return jsonify({'ok': True, 'uploaded_files': count, 'size_bytes': size_bytes})


@bp.post('/upload/finish/<int:job_id>')
def finish_batch_upload(job_id):
    init_tables()
    with db() as conn:
        job = conn.execute('SELECT * FROM photo_upload_jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['status'] != 'Uploading':
            return jsonify({'ok': False, 'error': 'Upload job not found or already queued.'}), 409
        total = conn.execute('SELECT COUNT(*) AS n FROM photo_upload_items WHERE job_id=?', (job_id,)).fetchone()['n']
        if not total:
            return jsonify({'ok': False, 'error': 'No photos were uploaded.'}), 400
        conn.execute('''UPDATE photo_upload_jobs SET status='Queued',total_files=?,uploaded_files=?,queued_at=? WHERE id=?''',
                     (total, total, datetime.now().isoformat(timespec='seconds'), job_id))
    return jsonify({'ok': True, 'job_id': job_id, 'status': 'Queued', 'total_files': total})


@bp.get('/upload/status/<int:job_id>')
def batch_upload_status(job_id):
    init_tables()
    with db() as conn:
        payload = _job_payload(conn, job_id, include_items=True)
    if not payload:
        return jsonify({'ok': False, 'error': 'Upload job not found.'}), 404
    payload['ok'] = True
    return jsonify(payload)
