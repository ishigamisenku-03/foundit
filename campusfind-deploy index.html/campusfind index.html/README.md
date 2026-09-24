# CampusFind

An improved, responsive campus lost-and-found website built from the supplied HTML prototype. The interface is bundled into `index.html` for easy preview; `app.py` turns it into a **shared** community board backed by SQLite.

## What works

- Search, category/type filters, sorting, pagination, item detail pages, and rule-based suggested matches.
- Post found or lost items with optional photos; photos are resized and re-encoded to JPEG to remove EXIF metadata.
- Public listing links and **private management links**. A post owner can edit, mark reunited, delete, and check a private message inbox without publishing an email address.
- Visitors can send a private message with a reply email. Messages are stored for the owner to check; **no email notifications are sent automatically**.
- Responsive desktop/mobile design, keyboard-accessible native dialogs, clear fictional-sample labels, and basic validation/rate limiting.

### Two modes

| How opened | Behavior |
| --- | --- |
| Open `index.html` directly | Offline, **browser-local preview** with fictional samples. Local posts do not sync between devices. |
| Serve with `app.py` | **Shared board**; posts, messages, and photos persist in the configured SQLite data directory and are accessible to other visitors. |

In the provided live preview `CAMPUSFIND_DEMO=1`, so six fictional example items are labelled **Fictional sample**. Set `CAMPUSFIND_DEMO=0` for a real, empty board. Never mistake sample listings for genuine lost property.

## Run locally

```bash
cd campusfind
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
CAMPUSFIND_DEMO=1 python app.py
```

Open `http://localhost:8001`. On Windows, activate with `.venv\Scripts\activate` and set environment variables using your shell's syntax.

To modify the site, edit `source/page.html`, `source/styles.css`, or `source/client.js`, then run `python build.py`. The server serves the generated `index.html`. To run tests, install `pytest` in the virtual environment and run `python -m pytest -q`.

## Deploy to a permanent URL

**A public host/account has not been connected to this workspace; the live preview is temporary, not a permanent deployment.** You can deploy the Docker app to a host that supports a **persistent writable volume**, such as a suitable paid Render/Railway/Fly.io configuration:

1. Push this project to your own repository. Create a Docker web service from the repository (`Dockerfile` is included).
2. Attach a **persistent volume mounted at `/data`**; set `CAMPUSFIND_DATA_DIR=/data` and `CAMPUSFIND_DEMO=0` in the hosting environment. The container listens on the provider's `PORT` (default 8000); health endpoint: `/health`.
3. Enable HTTPS (normally provided by the host) and connect your domain. Back up both `/data/campusfind.sqlite3` and `/data/uploads/` regularly.
4. Before promoting beyond a small pilot, add campus authentication or staff moderation, stronger abuse controls, privacy/retention policy, and email delivery if owners should receive notifications.

Run via Docker locally with persistent data:

```bash
docker build -t campusfind .
docker volume create campusfind-data
docker run --rm -p 8000:8000 -e CAMPUSFIND_DEMO=1 \
  -v campusfind-data:/data campusfind
```

**Important:** a static host such as GitHub Pages or a plain HTML upload can only run the offline/local preview; it cannot host the shared SQLite API. A container on ephemeral storage will lose posts, uploaded photos and messages on restart or redeploy. Do not publish a shared board without durable storage.

## Privacy and safety

Anyone with the site link can view public listings and any optional **public** contact email the poster provides. A private message, including the sender's reply email, is visible only to someone with that post's unguessable management token. Management tokens are stored in the poster's browser and can be saved as a private URL fragment; **keep that link secret**. Clearing browser storage without saving the link will lose owner access. Owners must check their inbox on the site; the application does not send email or verify identities/ownership. Arrange handovers at a staffed campus desk and keep personal IDs out of photos and posts.

This is a functional MVP/pilot, **not** a finished campus-wide identity or moderation system. It includes input validation, a 5 MB image limit, image re-encoding, basic per-IP rate limiting, and security-related response headers, but a public deployment still needs an operator and an abuse/privacy plan.

## Files

- `index.html`: ready-to-open standalone webpage (local fallback + shared-server client).
- `app.py`: Flask API and SQLite storage.
- `source/` and `build.py`: editable frontend and single-file bundler.
- `Dockerfile`, `requirements.txt`: deployment configuration.
- `tests/`: automated API tests.
- `prototype.html`: unchanged copy of the original supplied prototype, kept for reference.
