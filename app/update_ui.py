from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for


def version_tuple(value):
    try:
        return tuple(int(x) for x in str(value).strip().lstrip('v').split('.'))
    except Exception:
        return (0,)


def read_text(path, default=''):
    try:
        return path.read_text(errors='replace').strip()
    except OSError:
        return default


def tail(path, limit=80):
    try:
        return '\n'.join(path.read_text(errors='replace').splitlines()[-limit:])
    except OSError:
        return ''


def friendly_status(raw):
    raw = (raw or '').strip().lower()
    if raw in ('running', 'starting'):
        return 'Installing', 'working'
    if raw == 'queued':
        return 'Waiting to start', 'working'
    if raw == 'checking':
        return 'Checking for updates', 'working'
    if raw == 'complete':
        return 'Update finished', 'good'
    if raw.startswith('failed'):
        return 'Update needs attention', 'bad'
    return 'Ready', 'good'


def configure(app, updater_dir, current_version):
    def latest_version():
        return read_text(updater_dir / 'latest_version', '') or None

    def request_check():
        try:
            (updater_dir / 'check.request').write_text(datetime.now().isoformat(timespec='seconds'))
            return True
        except OSError:
            return False

    def updates_view():
        latest = latest_version()
        raw_status = read_text(updater_dir / 'status', 'idle') or 'idle'
        status_label, status_kind = friendly_status(raw_status)
        update_available = bool(latest and version_tuple(latest) > version_tuple(current_version))
        return render_template(
            'updates.html',
            current_version=current_version,
            latest_version=latest,
            update_available=update_available,
            updater_status=raw_status,
            status_label=status_label,
            status_kind=status_kind,
            update_log=tail(updater_dir / 'update.log'),
            checked_at=read_text(updater_dir / 'last_check', datetime.now().strftime('%H:%M:%S')),
        )

    def install_view():
        latest = latest_version()
        if not latest:
            request_check()
            flash('The updater has not completed a GitHub check yet. Press Check Again, then try once the latest version appears.', 'info')
            return redirect(url_for('updates'))
        if version_tuple(latest) <= version_tuple(current_version):
            flash('You already have the latest version.', 'info')
            return redirect(url_for('updates'))
        try:
            (updater_dir / 'status').write_text('queued')
            (updater_dir / 'update.request').write_text(datetime.now().isoformat(timespec='seconds'))
            flash(f'Update v{latest} has been queued. This page will refresh automatically.', 'success')
        except OSError as exc:
            flash(f'The update could not be started: {exc}', 'error')
        return redirect(url_for('updates'))

    def status_view():
        if request.args.get('check') == '1':
            request_check()
        latest = latest_version()
        raw_status = read_text(updater_dir / 'status', 'idle') or 'idle'
        status_label, status_kind = friendly_status(raw_status)
        return jsonify({
            'current': current_version,
            'latest': latest,
            'raw_status': raw_status,
            'status_label': status_label,
            'status_kind': status_kind,
            'update_available': bool(latest and version_tuple(latest) > version_tuple(current_version)),
            'log': tail(updater_dir / 'update.log', 24),
            'checked_at': read_text(updater_dir / 'last_check', ''),
        })

    app.view_functions['updates'] = updates_view
    app.view_functions['install_update'] = install_view
    if 'update_status_json' not in app.view_functions:
        app.add_url_rule('/updates/status.json', endpoint='update_status_json', view_func=status_view, methods=['GET'])
