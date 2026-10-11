"""Production entrypoint with migration-safe Authentik activation."""

import os

from flask import flash, redirect, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from app.main import app, initialise_owner_inventory, tenant, PUBLIC_ENDPOINTS
from app import server
from app.custom_fields import configure as configure_custom_fields
from app.dashboard_recent import configure as configure_dashboard_recent
from app.barcode_fixes import configure as configure_barcode_fixes


configure_custom_fields(app, server)
configure_dashboard_recent(app, server)
configure_barcode_fixes(app, server)

# Photo Shoot now stages files one request at a time, so the batch itself has no
# practical Flask-size ceiling. Keep only a generous per-file guard to prevent a
# malformed request from filling the server unexpectedly.
try:
    max_single_upload_gb = max(1, int(os.getenv('MAX_SINGLE_UPLOAD_GB', '8')))
except ValueError:
    max_single_upload_gb = 8
app.config['MAX_CONTENT_LENGTH'] = max_single_upload_gb * 1024 * 1024 * 1024


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    flash(f'That individual file is larger than the {max_single_upload_gb} GB per-file upload limit. Raise MAX_SINGLE_UPLOAD_GB if required.', 'error')
    return redirect(url_for('photoshoot.photo_shoot'))


def enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


# Existing installations keep their current local login during the one-time
# migration/rollback window. Once the owner deliberately enables Authentik,
# the OIDC layer becomes authoritative and fails closed if misconfigured.
if enabled("AUTH_ENABLED"):
    from app.oidc_auth import configure as configure_oidc
    configure_oidc(app, tenant, PUBLIC_ENDPOINTS, initialise_owner_inventory)