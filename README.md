# Inventory Manager v0.6.1

Self-hosted multi-account stock and sales manager designed for barcode-scanner Android devices, tablets and desktop browsers.

## Security model

Inventory Manager is a multi-account data application. Normal user authentication is delegated to **Authentik using OpenID Connect (OIDC)** when enabled. Inventory Manager does not create or store Authentik passwords. Authentik handles sign-in, password reset/recovery, MFA, account disabling and passkeys/WebAuthn when enabled there.

Each local account retains its own inventory database, photos and backups. Authenticated identities are permanently mapped with the OIDC `sub` claim; email is profile information only and is not the permanent identity key. Tenant selection happens on the server before database paths are resolved, so frontend filtering cannot grant access to another account's inventory.

## Install

```bash
git clone https://github.com/adamrfreeman644/stock-take.git
cd stock-take
cp .env.example .env
# Configure Authentik/OIDC and other deployment values.
docker compose up -d --build
```

Open `http://SERVER-IP:1975` on a trusted LAN or your HTTPS reverse-proxy URL.

Health check:

```text
http://SERVER-IP:1975/health
```

## Authentik setup

Create an OAuth2/OpenID Provider and Application in Authentik for Inventory Manager. Use Authorization Code flow and a confidential client. Set the redirect URI exactly to the externally reachable callback, for example:

```text
https://inventory.example.com/auth/callback
```

Copy `.env.example` to `.env` and set the OIDC values. For HTTPS deployments set `COOKIE_SECURE=true`.

Do not put the client secret in Git.

## Persistent data

- `./data/` — platform database plus isolated account databases
- `./photos/` — isolated account photo libraries and preserved crop originals
- `./backups/` — automatic, manual and pre-restore database backups

These mounts are separate from the application image, so rebuilding or updating the container does not replace inventory data.

## Shared updater

From v0.6.1, Inventory Manager **does not run its own `inventory-updater` container**.

Application updates are handled by the single **AD53 Shared App Updater** running on the Docker host.

The Inventory Manager container reaches it at:

```text
http://host.docker.internal:8093/apps/inventory-manager
```

This is controlled by:

```dotenv
SHARED_UPDATER_URL=http://host.docker.internal:8093/apps/inventory-manager
```

The in-app Updates page now reads update status from the shared updater and sends install requests to it. The Inventory Manager container itself does not need `/var/run/docker.sock`.

The shared updater:

1. Checks this repository's `VERSION` file.
2. Backs up the managed application source before updating.
3. Downloads the latest source from GitHub.
4. Runs preflight validation.
5. Rebuilds only the `inventory-manager` service.
6. Checks `/health` and confirms the running version.
7. Automatically restores the previous source/build if validation fails.

The old `updater.sh` remains in repository history only for older deployments. New deployments should not run the old `inventory-updater` service.

## Adding Inventory Manager to AD53 Shared App Updater

The shared updater is maintained with Immich Upload Gateway and its example registry already contains an `inventory-manager` entry.

The updater host must mount this project directory as:

```text
/apps/inventory-manager
```

Typical Unraid host path:

```text
/mnt/user/appdata/stock-take
```

After changing the shared updater registry/configuration, recreate the updater container and confirm:

```bash
curl http://127.0.0.1:8093/apps/inventory-manager/status
```

## Existing inventory features

The release preserves parent products with unique physical-item barcodes, quantity tracking, PayPal POS CSV import/export, dashboards, permanent sales history, events/pop-up shops, activity/undo, barcode search, archive/restore, automatic/manual backups, photo management, Photo Shoot workflow and existing inventory behaviour.

## Updating

Open the application's Updates page and press **Check Again** or **Install Update** as normal. Those controls now communicate with the central AD53 Shared App Updater.

If the shared updater cannot be reached, the Updates page reports that the updater is unavailable rather than writing request files to a local sidecar.

## Troubleshooting updates

Check the shared updater directly:

```bash
curl http://127.0.0.1:8093/apps/inventory-manager/status
```

Check Inventory Manager health:

```bash
curl http://127.0.0.1:1975/health
```

Check the central updater logs:

```bash
docker logs ad53-shared-updater
```

There should no longer be an `inventory-updater` container in a current deployment.
