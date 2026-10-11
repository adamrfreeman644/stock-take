import json
import os
import hashlib
import shutil
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, redirect, url_for, flash, send_file
from PIL import Image, ExifTags, ImageOps
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
            scanned_barcode TEXT,
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
            content_hash TEXT,
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
        scan_columns = {row['name'] for row in conn.execute('PRAGMA table_info(photo_shoot_scans)').fetchall()}
        if 'scanned_barcode' not in scan_columns:
            conn.execute('ALTER TABLE photo_shoot_scans ADD COLUMN scanned_barcode TEXT')
        photo_columns = {row['name'] for row in conn.execute('PRAGMA table_info(photos)').fetchall()}
        if 'source_barcode' not in photo_columns:
            conn.execute('ALTER TABLE photos ADD COLUMN source_barcode TEXT')
        session_columns = {row['name'] for row in conn.execute('PRAGMA table_info(photo_shoot_sessions)').fetchall()}
        if 'deleted_at' not in session_columns:
            conn.execute('ALTER TABLE photo_shoot_sessions ADD COLUMN deleted_at TEXT')
        if 'scanner_token' not in session_columns:
            conn.execute('ALTER TABLE photo_shoot_sessions ADD COLUMN scanner_token TEXT')
        if 'scanner_claimed_at' not in session_columns:
            conn.execute('ALTER TABLE photo_shoot_sessions ADD COLUMN scanner_claimed_at TEXT')
        if 'scanner_last_seen' not in session_columns:
            conn.execute('ALTER TABLE photo_shoot_sessions ADD COLUMN scanner_last_seen TEXT')
        item_columns = {row['name'] for row in conn.execute('PRAGMA table_info(photo_upload_items)').fetchall()}
        if 'content_hash' not in item_columns:
            conn.execute('ALTER TABLE photo_upload_items ADD COLUMN content_hash TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_photo_items_hash ON photo_upload_items(content_hash)')
        job_columns = {row['name'] for row in conn.execute('PRAGMA table_info(photo_upload_jobs)').fetchall()}
        if 'batch_token' not in job_columns:
            conn.execute('ALTER TABLE photo_upload_jobs ADD COLUMN batch_token TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_photo_jobs_batch ON photo_upload_jobs(batch_token,session_id)')


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
                    'INSERT INTO photos(product_id,filename,sort_order,source_barcode) VALUES(?,?,?,?)',
                    (product_id, filename, order, code),
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
            known = conn.execute('''SELECT s.scanned_at,p.item,p.main_colour,p.secondary_colour,p.pattern,s.product_id,
                                    COALESCE(s.scanned_barcode,(SELECT ib.barcode FROM item_barcodes ib WHERE ib.product_id=p.id ORDER BY ib.id LIMIT 1)) AS inventory_id,
                                    0 AS pending
                                    FROM photo_shoot_scans s JOIN products p ON p.id=s.product_id
                                    WHERE s.session_id=?''', (session['id'],)).fetchall()
            waiting = conn.execute('''SELECT scanned_at,'Pending product' AS item,'' AS main_colour,'' AS secondary_colour,'' AS pattern,NULL AS product_id,barcode AS inventory_id,1 AS pending
                                      FROM photo_shoot_pending_scans
                                      WHERE session_id=? AND resolved_product_id IS NULL''', (session['id'],)).fetchall()
            scans = []
            for row in known:
                item = dict(row)
                tag_rows = conn.execute('SELECT barcode FROM item_barcodes WHERE product_id=? ORDER BY id', (row['product_id'],)).fetchall()
                item['tags'] = [tag['barcode'] for tag in tag_rows]
                scans.append(item)
            for row in waiting:
                item = dict(row)
                item['tags'] = [item['inventory_id']]
                scans.append(item)
            scans.sort(key=lambda r: r['scanned_at'], reverse=True)
            current = scans[0] if scans else None
        recent = conn.execute("SELECT * FROM photo_shoot_sessions WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 8").fetchall()
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


def _result_data(raw):
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


@bp.get('/session/<int:session_id>')
def session_detail(session_id):
    init_tables()
    with db() as conn:
        shoot = conn.execute(
            'SELECT * FROM photo_shoot_sessions WHERE id=? AND deleted_at IS NULL',
            (session_id,),
        ).fetchone()
        if not shoot:
            return jsonify({'ok': False, 'error': 'Shoot not found.'}), 404

        matched = {}
        scan_rows = conn.execute(
            '''SELECT s.product_id,s.scanned_at,p.item,
                      COALESCE(s.scanned_barcode,(SELECT ib.barcode FROM item_barcodes ib
                       WHERE ib.product_id=p.id ORDER BY ib.id LIMIT 1)) AS barcode,
                      s.id AS marker_id
               FROM photo_shoot_scans s
               JOIN products p ON p.id=s.product_id
               WHERE s.session_id=?
               UNION ALL
               SELECT ps.resolved_product_id AS product_id,ps.scanned_at,p.item,
                      ps.barcode,ps.id AS marker_id
               FROM photo_shoot_pending_scans ps
               JOIN products p ON p.id=ps.resolved_product_id
               WHERE ps.session_id=? AND ps.resolved_product_id IS NOT NULL
               ORDER BY scanned_at,marker_id''',
            (session_id, session_id),
        ).fetchall()
        for row in scan_rows:
            key = int(row['product_id'])
            matched.setdefault(key, {
                'product_id': key,
                'item': row['item'],
                'barcode': row['barcode'] or '',
                'scanned_at': row['scanned_at'],
                'photos': [],
            })

        assigned = conn.execute(
            '''SELECT i.id AS upload_item_id,j.id AS job_id,i.result_json,ph.filename,ph.product_id
               FROM photo_upload_jobs j
               JOIN photo_upload_items i ON i.job_id=j.id AND i.status='Assigned'
               LEFT JOIN photos ph
                 ON ph.filename LIKE ('%_shoot' || j.id || '_' || i.id || '.%')
               WHERE j.session_id=?
               ORDER BY i.order_index,i.id''',
            (session_id,),
        ).fetchall()
        for row in assigned:
            data = _result_data(row['result_json'])
            barcode = str(data.get('inventory_id') or '')
            target = matched.get(int(row['product_id'])) if row['product_id'] is not None else next(
                (item for item in matched.values() if item['barcode'] == barcode),
                None,
            )
            if target and row['filename']:
                target['photos'].append({
                    'thumbnail_url': url_for('thumbnail_file', size='selector', filename=row['filename']),
                    'full_url': url_for('photo_file', filename=row['filename']),
                })

        claimed = conn.execute(
            '''SELECT a.id AS asset_id,a.product_id,ph.filename
               FROM photo_pending_assets a
               JOIN photo_upload_jobs j ON j.id=a.job_id
               LEFT JOIN photos ph
                 ON ph.filename LIKE ('%_pending' || a.id || '.%')
               WHERE j.session_id=? AND a.product_id IS NOT NULL
               ORDER BY a.id''',
            (session_id,),
        ).fetchall()
        for row in claimed:
            target = matched.get(int(row['product_id']))
            if target and row['filename']:
                target['photos'].append({
                    'thumbnail_url': url_for('thumbnail_file', size='selector', filename=row['filename']),
                    'full_url': url_for('photo_file', filename=row['filename']),
                })

        pending_rows = conn.execute(
            '''SELECT ps.id AS pending_scan_id,ps.barcode,ps.scanned_at,
                      a.id AS asset_id
               FROM photo_shoot_pending_scans ps
               LEFT JOIN photo_pending_assets a
                 ON a.pending_scan_id=ps.id AND a.product_id IS NULL
               WHERE ps.session_id=? AND ps.resolved_product_id IS NULL
               ORDER BY ps.scanned_at,ps.id,a.id''',
            (session_id,),
        ).fetchall()
        unmatched = {}
        for row in pending_rows:
            barcode = row['barcode']
            item = unmatched.setdefault(barcode, {
                'barcode': barcode,
                'scanned_at': row['scanned_at'],
                'photos': [],
                'create_url': url_for('new_product', barcode=barcode),
            })
            if row['asset_id'] and not any(photo['asset_id'] == int(row['asset_id']) for photo in item['photos']):
                item['photos'].append({
                    'asset_id': int(row['asset_id']),
                    'thumbnail_url': url_for('photoshoot.pending_thumbnail', asset_id=row['asset_id']),
                })

    return jsonify({
        'ok': True,
        'shoot': {
            'id': int(shoot['id']),
            'started_at': shoot['started_at'],
            'ended_at': shoot['ended_at'],
            'status': shoot['status'],
        },
        'matched': list(matched.values()),
        'unmatched': list(unmatched.values()),
    })


@bp.get('/pending-thumbnail/<int:asset_id>')
def pending_thumbnail(asset_id):
    init_tables()
    with db() as conn:
        asset = conn.execute(
            '''SELECT a.* FROM photo_pending_assets a
               WHERE a.id=? AND a.product_id IS NULL''',
            (asset_id,),
        ).fetchone()
    if not asset:
        return jsonify({'ok': False, 'error': 'Pending photo not found.'}), 404

    source = _staging_dir(asset['job_id']) / asset['staged_name']
    if not source.is_file():
        return jsonify({'ok': False, 'error': 'Pending photo file is missing.'}), 404
    cache_dir = source.parent / '.thumbnails'
    cache_dir.mkdir(parents=True, exist_ok=True)
    thumbnail = cache_dir / f'pending-{asset_id}.jpg'
    try:
        if not thumbnail.exists() or thumbnail.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode != 'RGB':
                    image = image.convert('RGB')
                image.thumbnail((160, 160), Image.Resampling.LANCZOS)
                temporary = thumbnail.with_suffix('.tmp')
                image.save(temporary, format='JPEG', quality=78, optimize=True, progressive=True)
                temporary.replace(thumbnail)
    except (OSError, ValueError):
        return jsonify({'ok': False, 'error': 'Pending photo could not be previewed.'}), 422
    return send_file(thumbnail, mimetype='image/jpeg', max_age=86400)


@bp.post('/session/<int:session_id>/delete')
def delete_session(session_id):
    init_tables()
    expected = f'DELETE SHOOT {session_id}'
    confirmation = request.form.get('confirmation', '').strip()
    preserve = request.form.get('preserve_photos') == 'yes'
    if confirmation != expected or not preserve:
        flash(f'The shoot was not deleted. Enter {expected} and confirm that product photos must be kept.', 'error')
        return redirect(url_for('photoshoot.photo_shoot'))

    with db() as conn:
        shoot = conn.execute(
            "SELECT * FROM photo_shoot_sessions WHERE id=? AND status='Ended' AND deleted_at IS NULL",
            (session_id,),
        ).fetchone()
        if not shoot:
            flash('Only an ended, visible shoot can be deleted.', 'error')
            return redirect(url_for('photoshoot.photo_shoot'))
        conn.execute(
            'UPDATE photo_shoot_sessions SET deleted_at=? WHERE id=?',
            (datetime.now().isoformat(timespec='seconds'), session_id),
        )
    flash(f'Shoot #{session_id} was removed from Recent Shoots. Assigned and pending photos were kept.', 'success')
    return redirect(url_for('photoshoot.photo_shoot'))


@bp.post('/session/<int:session_id>/pending/<path:barcode>/discard')
def discard_pending_item(session_id, barcode):
    init_tables()
    if request.form.get('confirmation', '').strip() != 'DISCARD':
        flash('Pending photos were not discarded. Type DISCARD to confirm.', 'error')
        return redirect(url_for('photoshoot.photo_shoot'))

    staged_files = []
    with db() as conn:
        rows = conn.execute(
            '''SELECT a.id,a.job_id,a.staged_name,a.upload_item_id
               FROM photo_pending_assets a
               JOIN photo_shoot_pending_scans ps ON ps.id=a.pending_scan_id
               WHERE ps.session_id=? AND ps.barcode=? AND a.product_id IS NULL''',
            (session_id, barcode),
        ).fetchall()
        for row in rows:
            staged_files.append(_staging_dir(row['job_id']) / row['staged_name'])
            conn.execute(
                "UPDATE photo_upload_items SET status='Discarded',result_json=? WHERE id=?",
                (json.dumps({'reason': 'Discarded by owner'}, separators=(',', ':')), row['upload_item_id']),
            )
        conn.execute(
            '''DELETE FROM photo_pending_assets
               WHERE id IN (
                   SELECT a.id FROM photo_pending_assets a
                   JOIN photo_shoot_pending_scans ps ON ps.id=a.pending_scan_id
                   WHERE ps.session_id=? AND ps.barcode=? AND a.product_id IS NULL
               )''',
            (session_id, barcode),
        )
        conn.execute(
            '''DELETE FROM photo_shoot_pending_scans
               WHERE session_id=? AND barcode=? AND resolved_product_id IS NULL''',
            (session_id, barcode),
        )
    for path in staged_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    flash(f'Discarded held photos for barcode {barcode}.', 'success')
    return redirect(url_for('photoshoot.photo_shoot'))


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


SCANNER_LEASE_SECONDS = 45


def _scanner_lease_alive(shoot):
    if not shoot or not shoot['scanner_token']:
        return False
    raw = shoot['scanner_last_seen'] or shoot['scanner_claimed_at']
    if not raw:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() < SCANNER_LEASE_SECONDS
    except (TypeError, ValueError):
        return False


@bp.post('/scanner/status')
def scanner_status():
    init_tables()
    token = request.form.get('scanner_token', '').strip()[:120]
    with db() as conn:
        shoot = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if not shoot:
            return jsonify({'ok': True, 'active': False, 'owned': False, 'available': False})
        alive = _scanner_lease_alive(shoot)
        if shoot['scanner_token'] and not alive:
            conn.execute('UPDATE photo_shoot_sessions SET scanner_token=NULL,scanner_claimed_at=NULL,scanner_last_seen=NULL WHERE id=?', (shoot['id'],))
        owned = bool(alive and token and shoot['scanner_token'] == token)
        return jsonify({'ok': True, 'active': True, 'owned': owned, 'available': not alive, 'session_id': int(shoot['id'])})


@bp.post('/scanner/heartbeat')
def scanner_heartbeat():
    init_tables()
    token = request.form.get('scanner_token', '').strip()[:120]
    with db() as conn:
        shoot = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if not shoot or not token or shoot['scanner_token'] != token or not _scanner_lease_alive(shoot):
            return jsonify({'ok': False, 'error': 'Scanner lease expired.'}), 409
        conn.execute('UPDATE photo_shoot_sessions SET scanner_last_seen=? WHERE id=?', (datetime.now().isoformat(timespec='seconds'), shoot['id']))
    return jsonify({'ok': True})


@bp.post('/scanner/claim')
def claim_scanner():
    init_tables()
    token = request.form.get('scanner_token', '').strip()[:120]
    if not token:
        return jsonify({'ok': False, 'error': 'Scanner identity is missing.'}), 400
    with db() as conn:
        shoot = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if not shoot:
            return jsonify({'ok': False, 'error': 'There is no active Photo Shoot.'}), 409
        if shoot['scanner_token'] and shoot['scanner_token'] != token and _scanner_lease_alive(shoot):
            return jsonify({'ok': False, 'error': 'This Photo Shoot already has an active scanner.'}), 409
        now = datetime.now().isoformat(timespec='seconds')
        conn.execute('UPDATE photo_shoot_sessions SET scanner_token=?,scanner_claimed_at=?,scanner_last_seen=? WHERE id=?',
                     (token, now, now, shoot['id']))
    return jsonify({'ok': True, 'session_id': int(shoot['id'])})


@bp.post('/scanner/release')
def release_scanner():
    init_tables()
    token = request.form.get('scanner_token', '').strip()[:120]
    with db() as conn:
        shoot = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if shoot and token and shoot['scanner_token'] == token:
            conn.execute('UPDATE photo_shoot_sessions SET scanner_token=NULL,scanner_claimed_at=NULL,scanner_last_seen=NULL WHERE id=?', (shoot['id'],))
    return jsonify({'ok': True})


@bp.post('/scan')
def scan_product():
    init_tables()
    code = request.form.get('barcode', '').strip()
    scanner_token = request.form.get('scanner_token', '').strip()[:120]
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    def fail(message, status=409):
        if wants_json:
            return jsonify({'ok': False, 'error': message}), status
        flash(message, 'error')
        return redirect(url_for('photoshoot.photo_shoot'))
    if not code:
        return fail('Scan a barcode first.', 400)
    with db() as conn:
        session = conn.execute("SELECT * FROM photo_shoot_sessions WHERE status='Active' ORDER BY id DESC LIMIT 1").fetchone()
        if not session:
            return fail('Start a photo shoot first.')
        if not scanner_token or session['scanner_token'] != scanner_token or not _scanner_lease_alive(session):
            return fail('This device is not the active Photo Shoot scanner.')
        now = datetime.now().isoformat(timespec='seconds')
        conn.execute('UPDATE photo_shoot_sessions SET scanner_last_seen=? WHERE id=?', (now, session['id']))
        product = conn.execute('''SELECT p.* FROM item_barcodes ib JOIN products p ON p.id=ib.product_id
                                  WHERE ib.barcode=? LIMIT 1''', (code,)).fetchone()
        if not product:
            conn.execute('INSERT INTO photo_shoot_pending_scans(session_id,barcode,scanned_at) VALUES(?,?,?)',
                         (session['id'], code, now))
            if wants_json:
                return jsonify({'ok': True, 'pending': True, 'barcode': code, 'item': 'Pending product', 'scanned_at': now})
            flash(f'Barcode {code} is not in stock yet. Its photo window is being held until that product is created.', 'info')
            return redirect(url_for('photoshoot.photo_shoot'))
        conn.execute('INSERT INTO photo_shoot_scans(session_id,product_id,scanned_at,scanned_barcode) VALUES(?,?,?,?)',
                     (session['id'], product['id'], now, code))
        tags = [r['barcode'] for r in conn.execute('SELECT barcode FROM item_barcodes WHERE product_id=? ORDER BY id', (product['id'],)).fetchall()]
        if wants_json:
            return jsonify({'ok': True, 'pending': False, 'barcode': code, 'item': product['item'],
                            'main_colour': product['main_colour'] or '', 'secondary_colour': product['secondary_colour'] or '',
                            'pattern': product['pattern'] or '', 'tags': tags, 'scanned_at': now})
    return redirect(url_for('photoshoot.photo_shoot'))

@bp.post('/end')
def end_session():
    init_tables()
    now = datetime.now().isoformat(timespec='seconds')
    with db() as conn:
        conn.execute("UPDATE photo_shoot_sessions SET status='Ended', ended_at=?, scanner_token=NULL, scanner_claimed_at=NULL, scanner_last_seen=NULL WHERE status='Active'", (now,))
    flash('Photo shoot ended. Select the batch and StockTake will stage it safely before assigning in the background.', 'success')
    return redirect(url_for('photoshoot.photo_shoot'))



def _uploaded_taken_at(uploaded):
    """Read the original camera timestamp without trusting the selected shoot."""
    try:
        uploaded.stream.seek(0)
        with Image.open(uploaded.stream) as image:
            exif = image.getexif()
            candidates = []
            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                candidates.extend([exif_ifd.get(36867), exif_ifd.get(36868)])
            except Exception:
                pass
            tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()} if exif else {}
            candidates.extend([tags.get('DateTimeOriginal'), tags.get('DateTimeDigitized'), tags.get('DateTime')])
            for raw in candidates:
                if not raw:
                    continue
                for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                    try:
                        return datetime.strptime(str(raw), fmt), 'camera Date Taken'
                    except ValueError:
                        pass
    except Exception:
        pass
    finally:
        try:
            uploaded.stream.seek(0)
        except Exception:
            pass
    return None, None


@bp.post('/upload/auto-file')
def auto_stage_file():
    """Stage one image into the shoot whose time window contains its capture time."""
    init_tables()
    uploaded = request.files.get('photo')
    if not uploaded or not uploaded.filename:
        return jsonify({'ok': False, 'error': 'No photo was supplied.'}), 400
    original = secure_filename(uploaded.filename) or f'photo-{uuid.uuid4().hex}'
    if '.' not in original or original.rsplit('.', 1)[1].lower() not in ALLOWED_EXTENSIONS:
        return jsonify({'ok': False, 'error': f'{uploaded.filename}: unsupported image type'}), 400
    token = ''.join(c for c in request.form.get('batch_token', '') if c.isalnum() or c in '-_')[:80]
    if not token:
        return jsonify({'ok': False, 'error': 'Upload batch is missing.'}), 400
    try:
        order_index = int(request.form.get('order_index', '0'))
        modified_ms = float(request.form.get('modified_ms', '0') or 0)
        device_offset = int(request.form.get('device_tz_offset', '0'))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Invalid upload metadata.'}), 400

    taken, source = _uploaded_taken_at(uploaded)
    server_offset = datetime.now().astimezone().utcoffset()
    server_offset_minutes = int(server_offset.total_seconds() // 60) if server_offset else 0
    if taken:
        taken = taken + timedelta(minutes=device_offset + server_offset_minutes)
    if not taken:
        return jsonify({'ok': False, 'error': f'{uploaded.filename}: no original camera Date Taken metadata; not assigned'}), 422

    with db() as conn:
        shoots = conn.execute(
            """SELECT s.* FROM photo_shoot_sessions s
               WHERE s.deleted_at IS NULL AND s.ended_at IS NOT NULL
                 AND s.started_at<=? AND s.ended_at>?
               ORDER BY s.started_at DESC""",
            (taken.isoformat(timespec='seconds'), taken.isoformat(timespec='seconds')),
        ).fetchall()
        if len(shoots) != 1:
            message = 'does not fall inside a completed Photo Shoot' if not shoots else 'matches more than one Photo Shoot'
            return jsonify({'ok': False, 'error': f'{uploaded.filename}: {message}', 'taken': taken.isoformat(timespec='seconds')}), 422
        shoot = shoots[0]
        marker_count = conn.execute(
            '''SELECT
               (SELECT COUNT(*) FROM photo_shoot_scans WHERE session_id=?) +
               (SELECT COUNT(*) FROM photo_shoot_pending_scans WHERE session_id=?) AS n''',
            (shoot['id'], shoot['id']),
        ).fetchone()['n']
        if not marker_count:
            return jsonify({'ok': False, 'error': f'{uploaded.filename}: matching shoot has no scan markers'}), 422
        job = conn.execute(
            "SELECT * FROM photo_upload_jobs WHERE batch_token=? AND session_id=? AND status='Uploading' ORDER BY id DESC LIMIT 1",
            (token, shoot['id']),
        ).fetchone()
        if job:
            job_id = int(job['id'])
        else:
            cur = conn.execute(
                '''INSERT INTO photo_upload_jobs(session_id,status,device_tz_offset,created_at,batch_token)
                   VALUES(?, 'Uploading', ?, ?, ?)''',
                (shoot['id'], device_offset, datetime.now().isoformat(timespec='seconds'), token),
            )
            job_id = int(cur.lastrowid)

    uploaded.stream.seek(0)
    digest = hashlib.sha256()
    while True:
        chunk = uploaded.stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    content_hash = digest.hexdigest()
    uploaded.stream.seek(0)
    with db() as conn:
        duplicate = conn.execute(
            """SELECT i.id,i.status,i.job_id,j.status AS job_status,j.session_id
               FROM photo_upload_items i JOIN photo_upload_jobs j ON j.id=i.job_id
               WHERE i.content_hash=? AND i.status NOT IN ('Discarded','Unmatched')
               ORDER BY i.id LIMIT 1""",
            (content_hash,),
        ).fetchone()
    if duplicate:
        # If a browser/app interruption left this file staged in an unfinished batch,
        # adopt that job into the retry batch so auto-finish will queue it instead of
        # silently stranding the already-uploaded photo.
        if duplicate['status'] == 'Staged' and duplicate['job_status'] == 'Uploading' and int(duplicate['session_id']) == int(shoot['id']):
            with db() as conn:
                conn.execute('UPDATE photo_upload_jobs SET batch_token=? WHERE id=?', (token, duplicate['job_id']))
            return jsonify({'ok': True, 'duplicate': True, 'recovered': True, 'job_id': int(duplicate['job_id']), 'session_id': int(shoot['id']), 'taken': taken.isoformat(timespec='seconds')})
        return jsonify({'ok': True, 'duplicate': True, 'skipped': True, 'session_id': int(shoot['id']), 'taken': taken.isoformat(timespec='seconds')})

    suffix = Path(original).suffix.lower()
    staged_name = f'{order_index:06d}-{uuid.uuid4().hex}{suffix}'
    destination = _staging_dir(job_id) / staged_name
    uploaded.save(destination)
    with db() as conn:
        conn.execute(
            '''INSERT INTO photo_upload_items(job_id,order_index,original_name,staged_name,modified_ms,size_bytes,content_hash,status)
               VALUES(?,?,?,?,?,?,?,'Staged')''',
            (job_id, order_index, original, staged_name, modified_ms, destination.stat().st_size, content_hash),
        )
        conn.execute('UPDATE photo_upload_jobs SET uploaded_files=uploaded_files+1 WHERE id=?', (job_id,))
    return jsonify({'ok': True, 'job_id': job_id, 'session_id': int(shoot['id']), 'taken': taken.isoformat(timespec='seconds'), 'timestamp_source': source})


@bp.post('/upload/auto-finish')
def auto_finish_upload():
    init_tables()
    token = ''.join(c for c in request.form.get('batch_token', '') if c.isalnum() or c in '-_')[:80]
    if not token:
        return jsonify({'ok': False, 'error': 'Upload batch is missing.'}), 400
    queued = []
    with db() as conn:
        jobs = conn.execute(
            "SELECT id,session_id FROM photo_upload_jobs WHERE batch_token=? AND status='Uploading' ORDER BY id",
            (token,),
        ).fetchall()
        for job in jobs:
            total = conn.execute('SELECT COUNT(*) AS n FROM photo_upload_items WHERE job_id=?', (job['id'],)).fetchone()['n']
            if not total:
                continue
            conn.execute(
                '''UPDATE photo_upload_jobs SET status='Queued',total_files=?,uploaded_files=?,queued_at=? WHERE id=?''',
                (total, total, datetime.now().isoformat(timespec='seconds'), job['id']),
            )
            queued.append({'job_id': int(job['id']), 'session_id': int(job['session_id']), 'total_files': int(total)})
    if not queued:
        return jsonify({'ok': False, 'error': 'No photos matched a Photo Shoot.'}), 422
    return jsonify({'ok': True, 'jobs': queued, 'total_files': sum(j['total_files'] for j in queued)})


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
