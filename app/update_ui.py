import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for


def version_tuple(value):
    try:
        return tuple(int(x) for x in str(value).strip().lstrip('v').split('.'))
    except Exception:
        return (0,)


def friendly_status(raw):
    raw = (raw or '').strip().lower()
    if raw in ('running', 'starting'):
        return 'Installing', 'working'
    if raw == 'queued':
        return 'Waiting to start', 'working'
    if raw == 'checking':
        return 'Checking for updates', 'working'
    if raw in ('success', 'complete'):
        return 'Update finished', 'good'
    if raw.startswith('failed'):
        return 'Update needs attention', 'bad'
    return 'Ready', 'good'


def configure(app, updater_dir, current_version):
    base = os.environ.get('SHARED_UPDATER_URL', 'http://host.docker.internal:8093/apps/inventory-manager').rstrip('/')

    def shared_request(path, method='GET'):
        req = urllib.request.Request(base + path, method=method, headers={'User-Agent': 'inventory-manager'})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))

    def get_status():
        try:
            return shared_request('/status')
        except Exception as exc:
            return {
                'current': current_version,
                'latest': None,
                'update_available': False,
                'running': False,
                'last_result': 'failed',
                'last_log': '',
                'error': f'Shared updater unavailable: {exc}',
            }

    def updates_view():
        status = get_status()
        latest = status.get('latest') or None
        raw_status = 'running' if status.get('running') else (status.get('last_result') or 'idle')
        status_label, status_kind = friendly_status(raw_status)
        if status.get('error'):
            status_label, status_kind = 'Updater unavailable', 'bad'
        return render_template(
            'updates.html',
            current_version=status.get('current') or current_version,
            latest_version=latest,
            update_available=bool(status.get('update_available')),
            updater_status=raw_status,
            status_label=status_label,
            status_kind=status_kind,
            update_log=status.get('last_log', '') or status.get('error', ''),
            checked_at=datetime.now().strftime('%H:%M:%S'),
        )

    def install_view():
        status = get_status()
        latest = status.get('latest')
        if status.get('error'):
            flash(status['error'], 'error')
            return redirect(url_for('updates'))
        if not latest or not status.get('update_available'):
            flash('You already have the latest version.', 'info')
            return redirect(url_for('updates'))
        try:
            response = shared_request('/install', method='POST')
            flash(response.get('message') or f'Update v{latest} has been queued.', 'success')
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode('utf-8')).get('message') or str(exc)
            except Exception:
                detail = str(exc)
            flash(f'The update could not be started: {detail}', 'error')
        except Exception as exc:
            flash(f'The shared updater could not be reached: {exc}', 'error')
        return redirect(url_for('updates'))

    def status_view():
        status = get_status()
        raw_status = 'running' if status.get('running') else (status.get('last_result') or 'idle')
        status_label, status_kind = friendly_status(raw_status)
        if status.get('error'):
            status_label, status_kind = 'Updater unavailable', 'bad'
        return jsonify({
            'current': status.get('current') or current_version,
            'latest': status.get('latest'),
            'raw_status': raw_status,
            'status_label': status_label,
            'status_kind': status_kind,
            'update_available': bool(status.get('update_available')),
            'log': status.get('last_log', '') or status.get('error', ''),
            'checked_at': datetime.now().strftime('%H:%M:%S'),
        })

    app.view_functions['updates'] = updates_view
    app.view_functions['install_update'] = install_view
    if 'update_status_json' not in app.view_functions:
        app.add_url_rule('/updates/status.json', endpoint='update_status_json', view_func=status_view, methods=['GET'])
