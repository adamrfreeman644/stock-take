import json
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ExifTags

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

from app import tenant

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif'}
POLL_SECONDS = max(1, int(os.getenv('PHOTO_WORKER_POLL_SECONDS', '2')))


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def parse_exif_datetime(raw):
    if not raw:
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            pass
    return None


def exif_taken_at(path):
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            except Exception:
                exif_ifd = {}
            for key in (36867, 36868):
                taken = parse_exif_datetime(exif_ifd.get(key))
                if taken:
                    return taken
            tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            return parse_exif_datetime(tags.get('DateTimeOriginal') or tags.get('DateTimeDigitized') or tags.get('DateTime'))
    except Exception:
        return None


def account_ids():
    with tenant.platform_db() as conn:
        return [int(r['id']) for r in conn.execute('SELECT id FROM accounts ORDER BY id').fetchall()]


def recover_processing_jobs(conn):
    conn.execute("UPDATE photo_upload_jobs SET status='Queued',started_at=NULL WHERE status='Processing'")


def next_job(account_id):
    db_path = tenant.account_db_path(account_id)
    if not db_path.exists():
        return None
    try:
        with connect(db_path) as conn:
            recover_processing_jobs(conn)
            row = conn.execute("SELECT id FROM photo_upload_jobs WHERE status='Queued' ORDER BY id LIMIT 1").fetchone()
            return int(row['id']) if row else None
    except sqlite3.Error:
        return None


def result_json(**kwargs):
    return json.dumps(kwargs, separators=(',', ':'))


def _normalise_job_photo_order(conn, job_id):
    """Ensure photos added by this shoot job are ordered oldest-to-newest per product.

    The card/icon uses the first photo by sort_order, so this makes the first photo
    taken during an item's scan window the primary image, followed by photo 2, 3, etc.
    Existing photos that pre-date the shoot keep their relative order and shoot photos
    are appended after them.
    """
    rows = conn.execute(
        '''SELECT ph.id, ph.product_id, ph.sort_order, i.result_json, i.id AS upload_item_id
           FROM photos ph
           JOIN photo_upload_items i ON ph.filename LIKE ('p' || ph.product_id || '_shoot' || ? || '_' || i.id || '.%')
           WHERE i.job_id=? AND i.status='Assigned'
           ORDER BY ph.product_id, ph.id''',
        (job_id, job_id),
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
        grouped[int(row['product_id'])].append((taken, int(row['upload_item_id']), int(row['id'])))

    for product_id, photo_rows in grouped.items():
        # Keep any non-shoot photos first and append this shoot's images in capture order.
        job_photo_ids = [photo_id for _, _, photo_id in photo_rows]
        placeholders = ','.join('?' for _ in job_photo_ids)
        params = [product_id] + job_photo_ids
        base_count = conn.execute(
            f'SELECT COUNT(*) AS n FROM photos WHERE product_id=? AND id NOT IN ({placeholders})',
            params,
        ).fetchone()['n'] if job_photo_ids else 0

        ordered = sorted(
            photo_rows,
            key=lambda row: (row[0] is None, row[0] or datetime.max, row[1], row[2]),
        )
        for offset, (_, _, photo_id) in enumerate(ordered, start=int(base_count) + 1):
            conn.execute('UPDATE photos SET sort_order=? WHERE id=?', (offset, photo_id))

        # Compact all sort_order values so the first row is always the primary/icon photo.
        compact = conn.execute(
            'SELECT id FROM photos WHERE product_id=? ORDER BY sort_order,id',
            (product_id,),
        ).fetchall()
        for order, row in enumerate(compact, start=1):
            conn.execute('UPDATE photos SET sort_order=? WHERE id=?', (order, row['id']))


def process_job(account_id, job_id):
    db_path = tenant.account_db_path(account_id)
    photo_dir = tenant.account_photo_dir(account_id)
    staging_dir = photo_dir / '.photo-shoot-staging' / f'job-{job_id}'

    with connect(db_path) as conn:
        job = conn.execute('SELECT * FROM photo_upload_jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['status'] not in ('Queued', 'Processing'):
            return
        conn.execute("UPDATE photo_upload_jobs SET status='Processing',started_at=?,error=NULL WHERE id=?",
                     (datetime.now().isoformat(timespec='seconds'), job_id))
        shoot = conn.execute('SELECT * FROM photo_shoot_sessions WHERE id=?', (job['session_id'],)).fetchone()
        known_rows = conn.execute('''SELECT s.id AS scan_id,s.scanned_at,s.product_id,p.item,
                                    (SELECT ib.barcode FROM item_barcodes ib WHERE ib.product_id=p.id ORDER BY ib.id LIMIT 1) AS inventory_id
                                    FROM photo_shoot_scans s JOIN products p ON p.id=s.product_id
                                    WHERE s.session_id=?''', (job['session_id'],)).fetchall()
        pending_rows = conn.execute('''SELECT ps.id AS pending_scan_id,ps.scanned_at,ps.barcode AS inventory_id,
                                             ps.resolved_product_id,p.item
                                      FROM photo_shoot_pending_scans ps
                                      LEFT JOIN products p ON p.id=ps.resolved_product_id
                                      WHERE ps.session_id=?''', (job['session_id'],)).fetchall()
        scans = []
        for row in known_rows:
            scans.append({
                'scanned_at': row['scanned_at'],
                'product_id': row['product_id'],
                'item': row['item'],
                'inventory_id': row['inventory_id'],
                'pending_scan_id': None,
            })
        for row in pending_rows:
            scans.append({
                'scanned_at': row['scanned_at'],
                'product_id': row['resolved_product_id'],
                'item': row['item'] or 'Pending product',
                'inventory_id': row['inventory_id'],
                'pending_scan_id': row['pending_scan_id'],
            })
        scans.sort(key=lambda s: s['scanned_at'])
        items = conn.execute("SELECT * FROM photo_upload_items WHERE job_id=? AND status='Staged' ORDER BY order_index,id", (job_id,)).fetchall()

    if not shoot or not scans:
        with connect(db_path) as conn:
            conn.execute("UPDATE photo_upload_jobs SET status='Failed',error=?,finished_at=? WHERE id=?",
                         ('Shoot or scan markers no longer exist.', datetime.now().isoformat(timespec='seconds'), job_id))
        return

    markers = [(datetime.fromisoformat(s['scanned_at']), s) for s in scans]
    session_end = datetime.fromisoformat(shoot['ended_at']) if shoot['ended_at'] else datetime.now()
    server_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    server_offset_minutes = int(server_offset.total_seconds() // 60)
    device_offset = int(job['device_tz_offset'] or 0)

    for item in items:
        staged = staging_dir / item['staged_name']
        if not staged.exists():
            with connect(db_path) as conn:
                conn.execute("UPDATE photo_upload_items SET status='Unmatched',result_json=? WHERE id=?",
                             (result_json(reason='Staged file is missing'), item['id']))
                conn.execute("UPDATE photo_upload_jobs SET processed_files=processed_files+1,unmatched_count=unmatched_count+1 WHERE id=?", (job_id,))
            continue

        taken = exif_taken_at(staged)
        source = 'camera Date Taken'
        if taken:
            taken = taken + timedelta(minutes=device_offset + server_offset_minutes)
        elif item['modified_ms']:
            try:
                taken = datetime.fromtimestamp(float(item['modified_ms']) / 1000.0)
                source = 'file timestamp fallback'
            except (ValueError, OSError, OverflowError):
                taken = None

        target = None
        if taken:
            for i, (start, scan) in enumerate(markers):
                end = markers[i + 1][0] if i + 1 < len(markers) else session_end
                if start <= taken < end:
                    target = dict(scan)
                    break

        if not taken or not target:
            reason = 'No readable camera or file timestamp' if not taken else f'Outside scan window ({taken.strftime("%Y-%m-%d %H:%M:%S")})'
            with connect(db_path) as conn:
                conn.execute("UPDATE photo_upload_items SET status='Unmatched',result_json=? WHERE id=?",
                             (result_json(reason=reason, timestamp_source=source), item['id']))
                conn.execute("UPDATE photo_upload_jobs SET processed_files=processed_files+1,unmatched_count=unmatched_count+1 WHERE id=?", (job_id,))
            continue

        ext = staged.suffix.lower().lstrip('.')
        if ext not in ALLOWED_EXTENSIONS:
            with connect(db_path) as conn:
                conn.execute("UPDATE photo_upload_items SET status='Unmatched',result_json=? WHERE id=?",
                             (result_json(reason='Unsupported image type'), item['id']))
                conn.execute("UPDATE photo_upload_jobs SET processed_files=processed_files+1,unmatched_count=unmatched_count+1 WHERE id=?", (job_id,))
            continue

        if target['product_id'] is None and target.get('pending_scan_id'):
            with connect(db_path) as conn:
                resolved = conn.execute('''SELECT ps.resolved_product_id,p.item
                                           FROM photo_shoot_pending_scans ps
                                           LEFT JOIN products p ON p.id=ps.resolved_product_id
                                           WHERE ps.id=?''', (target['pending_scan_id'],)).fetchone()
            if resolved and resolved['resolved_product_id']:
                target['product_id'] = int(resolved['resolved_product_id'])
                target['item'] = resolved['item'] or target['item']

        if target['product_id'] is None:
            with connect(db_path) as conn:
                existing = conn.execute('SELECT 1 FROM photo_pending_assets WHERE upload_item_id=? LIMIT 1', (item['id'],)).fetchone()
                if not existing:
                    conn.execute('''INSERT INTO photo_pending_assets
                           (pending_scan_id,barcode,job_id,upload_item_id,staged_name,original_name,created_at)
                           VALUES(?,?,?,?,?,?,?)''',
                        (target['pending_scan_id'], target['inventory_id'], job_id, item['id'], item['staged_name'], item['original_name'], datetime.now().isoformat(timespec='seconds')))
                conn.execute("UPDATE photo_upload_items SET status='PendingProduct',result_json=? WHERE id=?",
                    (result_json(inventory_id=target['inventory_id'], item='Pending product', pending_product=True,
                                 taken=taken.isoformat(timespec='seconds'), timestamp_source=source), item['id']))
                conn.execute('UPDATE photo_upload_jobs SET processed_files=processed_files+1 WHERE id=?', (job_id,))
            continue

        filename = f"p{target['product_id']}_shoot{job_id}_{item['id']}.{ext}"
        final_path = photo_dir / filename
        if not final_path.exists():
            shutil.copy2(staged, final_path)

        with connect(db_path) as conn:
            exists = conn.execute('SELECT 1 FROM photos WHERE filename=? LIMIT 1', (filename,)).fetchone()
            if not exists:
                order = conn.execute('SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM photos WHERE product_id=?', (target['product_id'],)).fetchone()['n']
                conn.execute('INSERT INTO photos(product_id,filename,sort_order) VALUES(?,?,?)', (target['product_id'], filename, order))
            conn.execute("UPDATE photo_upload_items SET status='Assigned',result_json=? WHERE id=?",
                         (result_json(inventory_id=target['inventory_id'], item=target['item'], taken=taken.isoformat(timespec='seconds'), timestamp_source=source), item['id']))
            conn.execute("UPDATE photo_upload_jobs SET processed_files=processed_files+1,assigned_count=assigned_count+1 WHERE id=?", (job_id,))
        try:
            staged.unlink()
        except OSError:
            pass

    with connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) AS n FROM photo_upload_items WHERE job_id=? AND status='Staged'", (job_id,)).fetchone()['n']
        if remaining == 0:
            _normalise_job_photo_order(conn, job_id)
            conn.execute("UPDATE photo_upload_jobs SET status='Complete',finished_at=? WHERE id=?",
                         (datetime.now().isoformat(timespec='seconds'), job_id))


def run_forever():
    print('StockTake Photo Worker started', flush=True)
    while True:
        worked = False
        for account_id in account_ids():
            job_id = next_job(account_id)
            if not job_id:
                continue
            worked = True
            try:
                process_job(account_id, job_id)
            except Exception as exc:
                try:
                    with connect(tenant.account_db_path(account_id)) as conn:
                        conn.execute("UPDATE photo_upload_jobs SET status='Failed',error=?,finished_at=? WHERE id=?",
                                     (str(exc)[:1000], datetime.now().isoformat(timespec='seconds'), job_id))
                except Exception:
                    pass
                print(f'Photo job {job_id} failed: {exc}', flush=True)
        if not worked:
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    run_forever()
