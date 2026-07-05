# SciToday PC Frontend V1.1.0

This package contains the static PC administration frontend in `admin_web/`.

## Deployment

The frontend uses same-origin `/api/*` requests and must be served by the
SciToday PC backend. Do not open `index.html` directly from the filesystem.

To upgrade an existing source or installed backend:

1. Stop the SciToday backend.
2. Replace its `admin_web/` directory with the directory from this package.
3. Restart the backend.
4. Open `/admin/` and perform a hard refresh in the browser.

The self-contained PC backend installer already includes this frontend. This
separate archive is intended for frontend-only source inspection or upgrades.

The package contains no API keys, authentication tokens, databases, logs,
private feeds, PDFs, or generated inbox content.
