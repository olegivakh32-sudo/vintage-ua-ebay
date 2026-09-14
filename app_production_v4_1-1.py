import os
import secrets
import html
import base64
import json
import re
import time
import requests
from io import BytesIO
from PIL import Image, ImageEnhance, ImageOps, ImageStat, ImageFilter

from flask import (
    Flask,
    redirect,
    request,
    session,
    Response,
)

app = Flask(__name__)

app.secret_key = os.environ.get(
    "APP_SECRET",
    "temporary-development-key"
)

# =========================================================
# CONFIG
# =========================================================

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN")
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
EBAY_RUNAME = os.environ.get("EBAY_RUNAME")
EBAY_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://vintage-ua-ebay-1.onrender.com/google/callback",
)

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)

GOOGLE_TOKEN_URL = (
    "https://oauth2.googleapis.com/token"
)

PICKER_SCOPE = (
    "https://www.googleapis.com/auth/"
    "photospicker.mediaitems.readonly"
)

PICKER_API = (
    "https://photospicker.googleapis.com/v1"
)

OPENAI_URL = (
    "https://api.openai.com/v1/responses"
)

PHOTO_CACHE = {}
PHOTO_EDIT_CACHE = {}
LOT002_DRAFT_CACHE = {}
ANALYSIS_CACHE = {}
IDENTIFICATION_CACHE = {}
MARKET_CACHE = {}
AUTO_FACT_CACHE = {}


# =========================================================
# SAFE PHOTO EDITOR
# =========================================================

def _white_background_edge_connected(image):
    """Replace only border-connected, background-like pixels with white.

    The mask is built on a smaller copy and then feathered. This deliberately
    avoids global colour replacement, so white/cream details inside the item
    are not erased merely because they are light coloured.
    """
    from collections import deque

    rgb = image.convert("RGB")
    w, h = rgb.size
    scale = min(1.0, 720.0 / max(w, h))
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    small = rgb.resize((sw, sh), Image.Resampling.LANCZOS)
    px = small.load()

    # Estimate background colour from many border samples, not one corner.
    samples = []
    step_x = max(1, sw // 24)
    step_y = max(1, sh // 24)
    for x in range(0, sw, step_x):
        samples.extend([px[x, 0], px[x, sh - 1]])
    for y in range(0, sh, step_y):
        samples.extend([px[0, y], px[sw - 1, y]])
    samples = [tuple(map(int, c)) for c in samples]
    med = tuple(sorted(c[i] for c in samples)[len(samples)//2] for i in range(3))

    # Adaptive tolerance: enough for paper/wall/table gradients, conservative
    # enough not to eat into a differently-coloured collectible.
    spread = sum(sum(abs(c[i] - med[i]) for i in range(3)) for c in samples) / max(1, len(samples))
    tol = max(42.0, min(92.0, 34.0 + spread * 0.75))

    def bg_like(c):
        # Euclidean-ish RGB distance plus channel max guard.
        d0, d1, d2 = int(c[0])-med[0], int(c[1])-med[1], int(c[2])-med[2]
        return (d0*d0 + d1*d1 + d2*d2) ** 0.5 <= tol and max(abs(d0), abs(d1), abs(d2)) <= tol * 0.9

    mask = Image.new("L", (sw, sh), 0)
    mp = mask.load()
    q = deque()

    # Seed only border pixels that resemble the estimated background.
    for x in range(sw):
        for y in (0, sh - 1):
            if mp[x, y] == 0 and bg_like(px[x, y]):
                mp[x, y] = 255; q.append((x, y))
    for y in range(sh):
        for x in (0, sw - 1):
            if mp[x, y] == 0 and bg_like(px[x, y]):
                mp[x, y] = 255; q.append((x, y))

    while q:
        x, y = q.popleft()
        for nx, ny in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
            if 0 <= nx < sw and 0 <= ny < sh and mp[nx, ny] == 0 and bg_like(px[nx, ny]):
                mp[nx, ny] = 255
                q.append((nx, ny))

    mask = mask.resize((w, h), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=max(0.8, max(w,h)/1800)))
    white = Image.new("RGB", (w, h), "white")
    return Image.composite(white, rgb, mask)


def _safe_photo_edit(image_bytes):
    """eBay-safe edit: white border-connected background + gentle correction.

    The collectible itself is not generatively redrawn and defects are not
    removed. Original Google Photos remain unchanged.
    """
    image = Image.open(BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)

    if image.mode not in ("RGB", "L"):
        if "A" in image.getbands():
            background = Image.new("RGB", image.size, "white")
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")

    # First isolate only edge-connected background and turn it pure white.
    image = _white_background_edge_connected(image)

    # Conservative corrections only: exposure, contrast, colour and sharpness.
    gray = image.convert("L")
    mean_luma = ImageStat.Stat(gray).mean[0]
    if mean_luma < 95:
        brightness = 1.12
    elif mean_luma < 120:
        brightness = 1.07
    elif mean_luma > 205:
        brightness = 0.98
    else:
        brightness = 1.02

    image = ImageEnhance.Brightness(image).enhance(brightness)
    image = ImageEnhance.Contrast(image).enhance(1.05)
    image = ImageEnhance.Color(image).enhance(1.02)
    image = ImageEnhance.Sharpness(image).enhance(1.08)

    max_side = 2400
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    out = BytesIO()
    image.save(out, format="JPEG", quality=93, optimize=True)
    return out.getvalue()


def _download_picker_photo(index, edited=False, max_side=2400):
    picker_id = session.get("picker_session_id")
    token = session.get("google_access_token")
    items = PHOTO_CACHE.get(picker_id, [])
    if not token or index < 1 or index > len(items):
        raise ValueError("Photo cache expired or photo not found")

    item = items[index - 1]
    base_url = item.get("mediaFile", {}).get("baseUrl")
    if not base_url:
        raise ValueError("Google Photos baseUrl missing")

    cache_key = (picker_id, index, max_side)
    if edited and cache_key in PHOTO_EDIT_CACHE:
        return PHOTO_EDIT_CACHE[cache_key]

    r = requests.get(
        base_url + f"=w{max_side}-h{max_side}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.content
    if edited:
        data = _safe_photo_edit(data)
        PHOTO_EDIT_CACHE[cache_key] = data
    return data


@app.route("/picker/editor")
def picker_editor():
    picker_id = session.get("picker_session_id")
    items = PHOTO_CACHE.get(picker_id, [])
    if not (1 <= len(items) <= 24):
        return page("Photo editor", "<h2>Select from 1 to 24 photos first.</h2><p><a href='/picker/start'>Select photos</a></p>"), 400

    cards = []
    for i in range(1, len(items) + 1):
        cards.append(f"""
        <div style='border:1px solid #ddd;border-radius:10px;padding:8px;margin-bottom:14px'>
          <h3 style='margin:4px 0 8px'>Photo {i}</h3>
          <label style='display:block;margin:6px 0 10px;font-size:17px;font-weight:bold'><input type='radio' name='main_photo' value='{i}' form='photo-approval-form' {'checked' if session.get('main_photo_index') == i else ''} required> MAIN PHOTO — use this as the first eBay photo</label>
          <div style='display:grid;grid-template-columns:1fr 1fr;gap:8px'>
            <div><b>Original</b><br><img src='/picker/thumb/{i}' style='width:100%;max-height:280px;object-fit:contain;background:#f4f4f4'></div>
            <div><b>Edited</b><br><img src='/picker/edited/{i}' loading='lazy' style='width:100%;max-height:280px;object-fit:contain;background:#f4f4f4'></div>
          </div>
        </div>""")

    approved = bool(session.get("photo_editor_approved"))
    status = "<p style='color:green'><b>✓ Edited photos are approved for eBay upload.</b></p>" if approved else "<p><b>Preview before approval.</b> After approval, these edited photos will be used for the eBay upload; the original Google Photos remain unchanged.</p>"
    return page("Current Lot Photo Editor", f"""
      <h2>Current Lot — Auto Photo Editor</h2>
      <p>Automatic eBay preparation: border-connected background is changed to pure white, then light/contrast/colour/sharpness are corrected gently. The item itself is not generatively redrawn. Scratches, cracks, rust, wear and other defects are NOT removed.</p>
      {status}
      {''.join(cards)}
      <form id='photo-approval-form' method='post' action='/picker/editor/approve'>
        <button type='submit' style='font-size:18px;padding:14px 18px'>Approve photos + MAIN PHOTO for eBay</button>
      </form>
      <p><a href='/picker/items'>Back to photos</a></p>
    """)


@app.route("/picker/edited/<int:index>")
def picker_edited(index):
    try:
        data = _download_picker_photo(index, edited=True, max_side=1200)
        return Response(data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    except Exception as exc:
        return f"Edited photo unavailable: {html.escape(str(exc))}", 404


@app.route("/picker/editor/approve", methods=["POST"])
def picker_editor_approve():
    picker_id = session.get("picker_session_id")
    items = PHOTO_CACHE.get(picker_id, [])
    if not (1 <= len(items) <= 24):
        return page("Photo editor", "<h2>Approval stopped: select from 1 to 24 active photos.</h2>"), 400
    try:
        main_index = int(request.form.get("main_photo") or 0)
    except Exception:
        main_index = 0
    if not (1 <= main_index <= len(items)):
        return page("Photo editor", "<h2>Select MAIN PHOTO before approval.</h2><p><a href='/picker/editor'>Back to photos</a></p>"), 400
    session["main_photo_index"] = main_index
    session["photo_order"] = _build_logical_photo_order(items, main_index)
    session["photo_editor_approved"] = True
    try:
        EBAY_MEDIA_CACHE.pop(_current_lot_sku(), None)
    except Exception:
        EBAY_MEDIA_CACHE.pop(LOT002_SKU, None) if 'LOT002_SKU' in globals() else None
    return redirect("/picker/items")


# =========================================================
# HTML HELPERS
# =========================================================

def page(title, body):
    # The original picker/UI was built around LOT 002. From LOT 003 onward,
    # keep the same workflow but render the active lot number everywhere in
    # the browser so the user never gets sent back visually to LOT 002.
    try:
        active_no = max(4, int(session.get("current_lot_number") or 4))
        active_label = f"LOT {active_no:03d}"
        if active_no >= 4:
            # Replace ONLY old hard-coded workflow labels. Never rewrite explicit
            # future labels such as START LOT 005, otherwise a correct next-lot
            # button is silently changed back to the current lot.
            legacy = re.compile(r"\bLOT\s+0*(?:2|3)\b")
            title = legacy.sub(active_label, str(title))
            body = legacy.sub(active_label, str(body))
    except Exception:
        pass

    return f"""
<!doctype html>
<html>
<head>
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >
    <title>{html.escape(title)}</title>
</head>

<body style="
    font-family:Arial,sans-serif;
    padding:18px;
    max-width:1000px;
    margin:auto;
    line-height:1.55;
">

{body}

</body>
</html>
"""


def text_to_html(text):
    return html.escape(text).replace("\n", "<br>")


def hidden_field(name, value):
    return (
        f'<textarea name="{html.escape(name)}" '
        f'style="display:none;">'
        f'{html.escape(value)}'
        f'</textarea>'
    )

# =========================================================
# EBAY HELPERS
# =========================================================

EBAY_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
]
def build_ebay_auth_url():
    params = {
        "client_id": EBAY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": EBAY_RUNAME,
        "scope": " ".join(EBAY_SCOPES),
    }

    return requests.Request(
        "GET",
        EBAY_AUTH_URL,
        params=params,
    ).prepare().url
@app.route("/ebay/connect")
def ebay_connect():
    return redirect(build_ebay_auth_url())
@app.route("/ebay/callback")
def ebay_callback():
    code = request.args.get("code")

    if not code:
        return "eBay authorization code not received", 400

    credentials = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    basic_auth = base64.b64encode(credentials.encode()).decode()

    response = requests.post(
        EBAY_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": EBAY_RUNAME,
        },
        timeout=30,
    )

    if not response.ok:
        return f"eBay token error: {html.escape(response.text)}", 400

    token_data = response.json()
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        return page(
            "eBay connected",
            """
            <h2>eBay connected successfully!</h2>
            <p>No refresh token was returned by eBay.</p>
            <p>Reconnect eBay and make sure the consent flow completes fully.</p>
            """,
        ), 400

    safe_refresh_token = html.escape(refresh_token, quote=True)

    return page(
        "eBay connected",
        f"""
        <h2>eBay connected successfully!</h2>
        <p>Copy the new refresh token and save it in Render as
        <b>EBAY_REFRESH_TOKEN</b>.</p>

        <input
            id="ebay-refresh-token"
            type="password"
            readonly
            value="{safe_refresh_token}"
            style="width:100%;font-size:16px;padding:10px;"
        >

        <p>
            <button
                type="button"
                onclick="navigator.clipboard.writeText(
                    document.getElementById('ebay-refresh-token').value
                ).then(() => {{
                    document.getElementById('copy-status').textContent =
                        'Copied ✓';
                }})"
                style="font-size:18px;padding:10px 16px;"
            >
                Copy refresh token
            </button>
            <span id="copy-status" style="margin-left:10px;"></span>
        </p>

        <p><b>Important:</b> after saving it in Render, redeploy the service,
        then open <code>/ebay/test-token</code>.</p>
        """,
    )


def get_ebay_access_token():
    credentials = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    basic_auth = base64.b64encode(credentials.encode()).decode()
    last_exc = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                EBAY_TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": EBAY_REFRESH_TOKEN,
                    "scope": " ".join(EBAY_SCOPES),
                },
                timeout=30,
            )
            if response.status_code in (502, 503, 504) and attempt < 3:
                time.sleep(attempt * 1.5)
                continue
            if not response.ok:
                raise RuntimeError(f"eBay refresh token error: {response.text}")
            return response.json()["access_token"]
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt >= 3:
                break
            time.sleep(attempt * 1.5)
    raise RuntimeError(f"Temporary network error while refreshing eBay token after 3 attempts: {last_exc}")
@app.route("/ebay/test-token")
def ebay_test_token():
    try:
        get_ebay_access_token()
        return "eBay token refresh OK"
    except Exception as exc:
        return f"eBay token refresh failed: {html.escape(str(exc))}", 500
        

@app.route("/ebay/policies")
def ebay_policies():
    """Read-only check of the seller's eBay US business policies."""
    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        policy_types = [
            ("Fulfillment / Shipping", "fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
            ("Payment", "payment_policy", "paymentPolicies", "paymentPolicyId"),
            ("Return", "return_policy", "returnPolicies", "returnPolicyId"),
        ]

        sections = []
        all_ok = True

        for label, endpoint, array_key, id_key in policy_types:
            url = f"https://api.ebay.com/sell/account/v1/{endpoint}"
            response = requests.get(
                url,
                headers=headers,
                params={"marketplace_id": "EBAY_US"},
                timeout=30,
            )

            if response.status_code != 200:
                all_ok = False
                sections.append(
                    f"<h2>{html.escape(label)}</h2>"
                    f"<p><b>ERROR HTTP {response.status_code}</b></p>"
                    f"<pre>{html.escape(response.text)}</pre>"
                )
                continue

            data = response.json()
            policies = data.get(array_key, [])

            if not policies:
                sections.append(
                    f"<h2>{html.escape(label)}</h2>"
                    "<p>No policies found for EBAY_US.</p>"
                )
                continue

            rows = []
            for policy in policies:
                name = html.escape(str(policy.get("name", "")))
                policy_id = html.escape(str(policy.get(id_key, "")))
                marketplace = html.escape(str(policy.get("marketplaceId", "")))
                rows.append(
                    "<tr>"
                    f"<td>{name}</td>"
                    f"<td><code>{policy_id}</code></td>"
                    f"<td>{marketplace}</td>"
                    "</tr>"
                )

            sections.append(
                f"<h2>{html.escape(label)}</h2>"
                "<table border='1' cellpadding='6' cellspacing='0'>"
                "<tr><th>Name</th><th>Policy ID</th><th>Marketplace</th></tr>"
                + "".join(rows)
                + "</table>"
            )

        status_text = "All eBay policy requests OK" if all_ok else "One or more eBay policy requests failed"
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>eBay Policies</title></head><body>"
            "<h1>eBay US Business Policies</h1>"
            f"<p><b>{html.escape(status_text)}</b></p>"
            + "".join(sections)
            + "</body></html>"
        )
        return page, (200 if all_ok else 502)

    except Exception as exc:
        return f"eBay policies check failed: {html.escape(str(exc))}", 500


@app.route("/ebay/programs")
def ebay_programs():
    """Read-only check of the seller's eBay Account API opt-in programs."""
    try:
        access_token = get_ebay_access_token()
        response = requests.get(
            "https://api.ebay.com/sell/account/v1/program/get_opted_in_programs",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

        if response.status_code != 200:
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>eBay Programs</title></head><body>"
                "<h1>eBay Opted-In Programs</h1>"
                f"<p><b>ERROR HTTP {response.status_code}</b></p>"
                f"<pre>{html.escape(response.text)}</pre>"
                "</body></html>",
                502,
            )

        data = response.json()
        programs = data.get("programs", [])

        rows = []
        selling_policy_enabled = False
        for program in programs:
            program_type = str(program.get("programType", ""))
            if program_type == "SELLING_POLICY_MANAGEMENT":
                selling_policy_enabled = True
            rows.append(
                "<tr>"
                f"<td><code>{html.escape(program_type)}</code></td>"
                "</tr>"
            )

        if rows:
            programs_html = (
                "<table border='1' cellpadding='6' cellspacing='0'>"
                "<tr><th>Program type</th></tr>"
                + "".join(rows)
                + "</table>"
            )
        else:
            programs_html = "<p>No opted-in programs returned by eBay.</p>"

        if selling_policy_enabled:
            result_html = (
                "<p><b>SELLING_POLICY_MANAGEMENT: ENABLED</b></p>"
                "<p>Your account is already opted in to Business Policies.</p>"
            )
        else:
            result_html = (
                "<p><b>SELLING_POLICY_MANAGEMENT: NOT ENABLED</b></p>"
                "<p>This confirms why the Business Policy requests return error 20403.</p>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>eBay Programs</title></head><body>"
            "<h1>eBay Opted-In Programs</h1>"
            + result_html
            + programs_html
            + "</body></html>"
        )

    except Exception as exc:
        return f"eBay programs check failed: {html.escape(str(exc))}", 500


@app.route("/ebay/opt-in", methods=["GET", "POST"])
def ebay_opt_in():
    """Require an explicit browser confirmation before opting in to eBay Business Policies."""
    if request.method == "GET":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Enable eBay Business Policies</title></head><body>"
            "<h1>Enable eBay Business Policies</h1>"
            "<p>This will opt your eBay seller account into "
            "<b>SELLING_POLICY_MANAGEMENT</b>.</p>"
            "<p>No listing or business policy will be created by this action.</p>"
            "<form method='post'>"
            "<button type='submit' style='padding:12px 18px;font-size:16px'>"
            "Confirm and enable Business Policies"
            "</button>"
            "</form>"
            "</body></html>"
        )

    try:
        access_token = get_ebay_access_token()
        response = requests.post(
            "https://api.ebay.com/sell/account/v1/program/opt_in",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"programType": "SELLING_POLICY_MANAGEMENT"},
            timeout=30,
        )

        if response.status_code in (200, 204):
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>eBay Business Policies Enabled</title></head><body>"
                "<h1>Business Policies enabled successfully</h1>"
                "<p><b>SELLING_POLICY_MANAGEMENT: ENABLED</b></p>"
                "<p><a href='/ebay/programs'>Verify program status</a></p>"
                "<p><a href='/ebay/policies'>Check eBay US policies</a></p>"
                "</body></html>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>eBay Opt-In Error</title></head><body>"
            "<h1>Business Policies opt-in failed</h1>"
            f"<p><b>ERROR HTTP {response.status_code}</b></p>"
            f"<pre>{html.escape(response.text)}</pre>"
            "</body></html>",
            502,
        )

    except Exception as exc:
        return f"eBay Business Policies opt-in failed: {html.escape(str(exc))}", 500



@app.route("/ebay/create-payment-policy", methods=["GET", "POST"])
def ebay_create_payment_policy():
    """Create one EBAY_US payment policy only after explicit browser confirmation."""
    policy_name = "Vintage UA eBay Payment"

    if request.method == "GET":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Create eBay Payment Policy</title></head><body>"
            "<h1>Create eBay US Payment Policy</h1>"
            f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
            "<p><b>Marketplace:</b> EBAY_US</p>"
            "<p><b>Category:</b> ALL_EXCLUDING_MOTORS_VEHICLES</p>"
            "<p><b>Immediate payment:</b> No</p>"
            "<p>This action creates one real Payment Policy in your eBay seller account. "
            "It does not create or publish a listing.</p>"
            "<form method='post'>"
            "<button type='submit' style='padding:12px 18px;font-size:16px'>"
            "Confirm and create Payment Policy"
            "</button></form>"
            "<p><a href='/ebay/policies'>Cancel / check current policies</a></p>"
            "</body></html>"
        )

    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Duplicate guard: do not create another policy with the same name.
        check = requests.get(
            "https://api.ebay.com/sell/account/v1/payment_policy",
            headers=headers,
            params={"marketplace_id": "EBAY_US"},
            timeout=30,
        )
        if check.status_code != 200:
            return (
                "<h1>Could not check existing Payment Policies</h1>"
                f"<p><b>ERROR HTTP {check.status_code}</b></p>"
                f"<pre>{html.escape(check.text)}</pre>",
                502,
            )

        for policy in check.json().get("paymentPolicies", []):
            if str(policy.get("name", "")).strip().casefold() == policy_name.casefold():
                policy_id = html.escape(str(policy.get("paymentPolicyId", "")))
                return (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>Payment Policy Already Exists</title></head><body>"
                    "<h1>Payment Policy already exists</h1>"
                    f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                    f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                    "<p>No duplicate was created.</p>"
                    "<p><a href='/ebay/policies'>Check all eBay US policies</a></p>"
                    "</body></html>"
                )

        payload = {
            "name": policy_name,
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
            "immediatePay": False,
        }
        response = requests.post(
            "https://api.ebay.com/sell/account/v1/payment_policy",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code in (200, 201):
            data = response.json() if response.text.strip() else {}
            policy_id = html.escape(str(data.get("paymentPolicyId", "")))
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Payment Policy Created</title></head><body>"
                "<h1>Payment Policy created successfully</h1>"
                f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                "<p><a href='/ebay/policies'>Verify eBay US policies</a></p>"
                "</body></html>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Payment Policy Error</title></head><body>"
            "<h1>Payment Policy creation failed</h1>"
            f"<p><b>ERROR HTTP {response.status_code}</b></p>"
            f"<pre>{html.escape(response.text)}</pre>"
            "</body></html>",
            502,
        )
    except Exception as exc:
        return f"eBay Payment Policy creation failed: {html.escape(str(exc))}", 500


@app.route("/ebay/create-return-policy", methods=["GET", "POST"])
def ebay_create_return_policy():
    """Create one EBAY_US no-returns policy only after explicit browser confirmation."""
    policy_name = "Vintage UA eBay No Returns"

    if request.method == "GET":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Create eBay Return Policy</title></head><body>"
            "<h1>Create eBay US Return Policy</h1>"
            f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
            "<p><b>Marketplace:</b> EBAY_US</p>"
            "<p><b>Category:</b> ALL_EXCLUDING_MOTORS_VEHICLES</p>"
            "<p><b>Returns accepted:</b> No</p>"
            "<p>This action creates one real Return Policy in your eBay seller account. "
            "It does not create or publish a listing.</p>"
            "<p><b>Important:</b> This seller policy does not remove buyer protections "
            "for items that are not as described or other cases covered by eBay rules.</p>"
            "<form method='post'>"
            "<button type='submit' style='padding:12px 18px;font-size:16px'>"
            "Confirm and create Return Policy"
            "</button></form>"
            "<p><a href='/ebay/policies'>Cancel / check current policies</a></p>"
            "</body></html>"
        )

    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Duplicate guard: do not create another policy with the same name.
        check = requests.get(
            "https://api.ebay.com/sell/account/v1/return_policy",
            headers=headers,
            params={"marketplace_id": "EBAY_US"},
            timeout=30,
        )
        if check.status_code != 200:
            return (
                "<h1>Could not check existing Return Policies</h1>"
                f"<p><b>ERROR HTTP {check.status_code}</b></p>"
                f"<pre>{html.escape(check.text)}</pre>",
                502,
            )

        for policy in check.json().get("returnPolicies", []):
            if str(policy.get("name", "")).strip().casefold() == policy_name.casefold():
                policy_id = html.escape(str(policy.get("returnPolicyId", "")))
                return (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>Return Policy Already Exists</title></head><body>"
                    "<h1>Return Policy already exists</h1>"
                    f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                    f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                    "<p>No duplicate was created.</p>"
                    "<p><a href='/ebay/policies'>Check all eBay US policies</a></p>"
                    "</body></html>"
                )

        payload = {
            "name": policy_name,
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
            "returnsAccepted": False,
        }
        response = requests.post(
            "https://api.ebay.com/sell/account/v1/return_policy",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code in (200, 201):
            data = response.json() if response.text.strip() else {}
            policy_id = html.escape(str(data.get("returnPolicyId", "")))
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Return Policy Created</title></head><body>"
                "<h1>Return Policy created successfully</h1>"
                f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                "<p><a href='/ebay/policies'>Verify eBay US policies</a></p>"
                "</body></html>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Return Policy Error</title></head><body>"
            "<h1>Return Policy creation failed</h1>"
            f"<p><b>ERROR HTTP {response.status_code}</b></p>"
            f"<pre>{html.escape(response.text)}</pre>"
            "</body></html>",
            502,
        )
    except Exception as exc:
        return f"eBay Return Policy creation failed: {html.escape(str(exc))}", 500


@app.route("/ebay/create-shipping-policy", methods=["GET", "POST"])
def ebay_create_shipping_policy():
    """Create the LOT 001 EBAY_US fulfillment policy only after explicit confirmation."""
    policy_name = "Vintage UA eBay Shipping USA 49.99"
    shipping_cost = "49.99"
    service_code = "StandardShippingFromOutsideUS"
    handling_days = 3

    if request.method == "GET":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Create eBay Shipping Policy</title></head><body>"
            "<h1>Create eBay US Shipping Policy</h1>"
            f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
            "<p><b>Marketplace:</b> EBAY_US</p>"
            "<p><b>Category:</b> ALL_EXCLUDING_MOTORS_VEHICLES</p>"
            "<p><b>Ship from:</b> Outside US (Ukraine item location will be set on the inventory location)</p>"
            f"<p><b>Shipping service:</b> {html.escape(service_code)}</p>"
            "<p><b>Cost type:</b> Flat rate</p>"
            f"<p><b>Buyer shipping cost:</b> ${shipping_cost} USD</p>"
            f"<p><b>Handling time:</b> {handling_days} business days</p>"
            "<p>This policy is intended for LOT 001. Future lots can use a different shipping policy/cost after their packed weight, dimensions and actual Ukraine-to-USA shipping cost are known.</p>"
            "<p>This action creates one real Fulfillment/Shipping Policy in your eBay seller account. It does not create or publish a listing.</p>"
            "<form method='post'>"
            "<button type='submit' style='padding:12px 18px;font-size:16px'>Confirm and create Shipping Policy</button>"
            "</form>"
            "<p><a href='/ebay/policies'>Cancel / check current policies</a></p>"
            "</body></html>"
        )

    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Duplicate guard: do not create another policy with the same name.
        check = requests.get(
            "https://api.ebay.com/sell/account/v1/fulfillment_policy",
            headers=headers,
            params={"marketplace_id": "EBAY_US"},
            timeout=30,
        )
        if check.status_code != 200:
            return (
                "<h1>Could not check existing Shipping Policies</h1>"
                f"<p><b>ERROR HTTP {check.status_code}</b></p>"
                f"<pre>{html.escape(check.text)}</pre>",
                502,
            )

        for policy in check.json().get("fulfillmentPolicies", []):
            if str(policy.get("name", "")).strip().casefold() == policy_name.casefold():
                policy_id = html.escape(str(policy.get("fulfillmentPolicyId", "")))
                return (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>Shipping Policy Already Exists</title></head><body>"
                    "<h1>Shipping Policy already exists</h1>"
                    f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                    f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                    "<p>No duplicate was created.</p>"
                    "<p><a href='/ebay/policies'>Check all eBay US policies</a></p>"
                    "</body></html>"
                )

        payload = {
            "name": policy_name,
            "marketplaceId": "EBAY_US",
            "categoryTypes": [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES"}],
            "handlingTime": {"value": handling_days, "unit": "DAY"},
            "shippingOptions": [
                {
                    "optionType": "DOMESTIC",
                    "costType": "FLAT_RATE",
                    "shippingServices": [
                        {
                            "sortOrder": 1,
                            "shippingServiceCode": service_code,
                            "shippingCost": {"value": shipping_cost, "currency": "USD"},
                            "additionalShippingCost": {"value": "0.00", "currency": "USD"},
                            "freeShipping": False,
                        }
                    ],
                }
            ],
            "globalShipping": False,
            "pickupDropOff": False,
            "freightShipping": False,
        }
        response = requests.post(
            "https://api.ebay.com/sell/account/v1/fulfillment_policy",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code in (200, 201):
            data = response.json() if response.text.strip() else {}
            policy_id = html.escape(str(data.get("fulfillmentPolicyId", "")))
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Shipping Policy Created</title></head><body>"
                "<h1>Shipping Policy created successfully</h1>"
                f"<p><b>Name:</b> {html.escape(policy_name)}</p>"
                f"<p><b>Policy ID:</b> <code>{policy_id}</code></p>"
                f"<p><b>Shipping cost:</b> ${shipping_cost} USD</p>"
                "<p><a href='/ebay/policies'>Verify eBay US policies</a></p>"
                "</body></html>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Shipping Policy Error</title></head><body>"
            "<h1>Shipping Policy creation failed</h1>"
            f"<p><b>ERROR HTTP {response.status_code}</b></p>"
            f"<pre>{html.escape(response.text)}</pre>"
            "</body></html>",
            502,
        )
    except Exception as exc:
        return f"eBay Shipping Policy creation failed: {html.escape(str(exc))}", 500


@app.route("/ebay/create-inventory-location", methods=["GET", "POST"])
def ebay_create_inventory_location():
    """Create one eBay Inventory API warehouse location after explicit confirmation."""
    merchant_location_key = "vintage-ua-warehouse-1"
    location_name = "Vintage UA Ukraine Warehouse"

    if request.method == "GET":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Create eBay Inventory Location</title></head><body>"
            "<h1>Create eBay Inventory Location</h1>"
            f"<p><b>Name:</b> {html.escape(location_name)}</p>"
            f"<p><b>Merchant location key:</b> <code>{html.escape(merchant_location_key)}</code></p>"
            "<p><b>Country:</b> Ukraine (UA)</p>"
            "<p><b>Type:</b> WAREHOUSE</p>"
            "<p><b>Status:</b> ENABLED</p>"
            "<p>Enter the 5-digit postal code of the physical place in Ukraine from which you actually ship eBay parcels.</p>"
            "<p>eBay requires a physical inventory location. The postal code is sent directly to eBay and is not stored in this app.</p>"
            "<form method='post'>"
            "<label><b>Shipping-from postal code:</b> "
            "<input name='postal_code' inputmode='numeric' pattern='[0-9]{5}' maxlength='5' required "
            "style='font-size:18px;padding:8px;width:120px'></label>"
            "<br><br>"
            "<button type='submit' style='padding:12px 18px;font-size:16px'>Confirm and create Inventory Location</button>"
            "</form>"
            "<p><a href='/ebay/inventory-locations'>Cancel / check current inventory locations</a></p>"
            "</body></html>"
        )

    postal_code = request.form.get("postal_code", "").strip()
    if len(postal_code) != 5 or not postal_code.isdigit():
        return (
            "<h1>Invalid postal code</h1>"
            "<p>Please enter the 5-digit postal code of your real shipping-from location in Ukraine.</p>"
            "<p><a href='/ebay/create-inventory-location'>Go back</a></p>",
            400,
        )

    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        location_url = (
            "https://api.ebay.com/sell/inventory/v1/location/"
            + merchant_location_key
        )

        # Duplicate guard: check this immutable merchantLocationKey first.
        existing = requests.get(location_url, headers=headers, timeout=30)
        if existing.status_code == 200:
            data = existing.json() if existing.text.strip() else {}
            status = html.escape(str(data.get("merchantLocationStatus", "")))
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Inventory Location Already Exists</title></head><body>"
                "<h1>Inventory Location already exists</h1>"
                f"<p><b>Merchant location key:</b> <code>{html.escape(merchant_location_key)}</code></p>"
                f"<p><b>Status:</b> {status or 'found'}</p>"
                "<p>No duplicate was created.</p>"
                "<p><a href='/ebay/inventory-locations'>Verify inventory locations</a></p>"
                "</body></html>"
            )
        if existing.status_code not in (404,):
            return (
                "<h1>Could not check Inventory Location</h1>"
                f"<p><b>ERROR HTTP {existing.status_code}</b></p>"
                f"<pre>{html.escape(existing.text)}</pre>",
                502,
            )

        payload = {
            "location": {
                "address": {
                    "postalCode": postal_code,
                    "country": "UA",
                }
            },
            "name": location_name,
            "merchantLocationStatus": "ENABLED",
            "locationTypes": ["WAREHOUSE"],
        }
        response = requests.post(
            location_url,
            headers=headers,
            json=payload,
            timeout=30,
        )

        if response.status_code in (200, 201, 204):
            return (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Inventory Location Created</title></head><body>"
                "<h1>Inventory Location created successfully</h1>"
                f"<p><b>Name:</b> {html.escape(location_name)}</p>"
                f"<p><b>Merchant location key:</b> <code>{html.escape(merchant_location_key)}</code></p>"
                "<p><b>Country:</b> Ukraine (UA)</p>"
                "<p><b>Type:</b> WAREHOUSE</p>"
                "<p><b>Status:</b> ENABLED</p>"
                "<p><a href='/ebay/inventory-locations'>Verify inventory locations</a></p>"
                "</body></html>"
            )

        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Inventory Location Error</title></head><body>"
            "<h1>Inventory Location creation failed</h1>"
            f"<p><b>ERROR HTTP {response.status_code}</b></p>"
            f"<pre>{html.escape(response.text)}</pre>"
            "</body></html>",
            502,
        )
    except Exception as exc:
        return f"eBay Inventory Location creation failed: {html.escape(str(exc))}", 500


@app.route("/ebay/inventory-locations")
def ebay_inventory_locations():
    """Read-only page listing eBay Inventory API locations."""
    try:
        access_token = get_ebay_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        response = requests.get(
            "https://api.ebay.com/sell/inventory/v1/location",
            headers=headers,
            params={"limit": 100, "offset": 0},
            timeout=30,
        )
        if response.status_code != 200:
            return (
                "<h1>Could not read Inventory Locations</h1>"
                f"<p><b>ERROR HTTP {response.status_code}</b></p>"
                f"<pre>{html.escape(response.text)}</pre>",
                502,
            )

        data = response.json() if response.text.strip() else {}
        locations = data.get("locations", [])
        parts = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<title>eBay Inventory Locations</title></head><body>",
            "<h1>eBay Inventory Locations</h1>",
            f"<p><b>Total returned:</b> {len(locations)}</p>",
        ]
        if not locations:
            parts.append("<p>No inventory locations found.</p>")
        for loc in locations:
            key = html.escape(str(loc.get("merchantLocationKey", "")))
            name = html.escape(str(loc.get("name", "")))
            status = html.escape(str(loc.get("merchantLocationStatus", "")))
            types = ", ".join(html.escape(str(x)) for x in loc.get("locationTypes", []))
            address = (loc.get("location") or {}).get("address") or {}
            country = html.escape(str(address.get("country", "")))
            postal = html.escape(str(address.get("postalCode", "")))
            parts.extend([
                "<hr>",
                f"<p><b>Name:</b> {name}</p>",
                f"<p><b>Merchant location key:</b> <code>{key}</code></p>",
                f"<p><b>Status:</b> {status}</p>",
                f"<p><b>Type:</b> {types}</p>",
                f"<p><b>Country:</b> {country}</p>",
                f"<p><b>Postal code:</b> {postal}</p>",
            ])
        parts.append("</body></html>")
        return "".join(parts)
    except Exception as exc:
        return f"eBay Inventory Location check failed: {html.escape(str(exc))}", 500


# =========================================================
# OPENAI HELPERS
# =========================================================

def extract_openai_text(data):
    direct = data.get("output_text")

    if direct:
        return direct

    texts = []

    for item in data.get("output", []):
        if item.get("type") != "message":
            continue

        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")

                if text:
                    texts.append(text)

    return "\n".join(texts)


def call_openai(payload, timeout=180):
    """Call OpenAI with small transient retry protection for read-only AI work."""
    last_error = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                last_error = RuntimeError(f"OpenAI HTTP {response.status_code}: {response.text[:1000]}")
                time.sleep(1.5 * attempt)
                continue
            if not response.ok:
                raise RuntimeError(f"OpenAI HTTP {response.status_code}\n" + response.text[:5000])
            data = response.json()
            result = extract_openai_text(data)
            if not result:
                raise RuntimeError("OpenAI returned no text.\n" + str(data)[:5000])
            return result
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            if attempt >= 3:
                break
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"OpenAI request failed after retries: {last_error}")

# =========================================================
# GOOGLE HELPERS
# =========================================================

def google_headers():
    token = session.get("google_access_token")

    return {
        "Authorization": f"Bearer {token}"
    }


def get_selected_items():
    """Return picked items only after Google says the Picker session is ready.

    Google Photos Picker requires clients to poll sessions.get until
    mediaItemsSet=true. Calling mediaItems.list earlier returns
    FAILED_PRECONDITION; that is a normal "selection still pending" state, not
    an expired session.
    """
    picker_id = session.get("picker_session_id")

    if not picker_id:
        return None, "NO_PICKER_SESSION"

    # 1) Check the session state first, as required by the Photos Picker API.
    status = requests.get(
        f"{PICKER_API}/sessions/{picker_id}",
        headers=google_headers(),
        timeout=30,
    )

    if status.status_code == 401:
        return None, "GOOGLE_AUTH_EXPIRED"
    if status.status_code in (403, 404):
        return None, "PICKER_SESSION_EXPIRED"
    if not status.ok:
        return None, f"PICKER_STATUS_ERROR HTTP {status.status_code}: {status.text}"

    status_data = status.json()
    if not status_data.get("mediaItemsSet"):
        polling = status_data.get("pollingConfig") or {}
        interval = str(polling.get("pollInterval") or "3s")
        return None, f"PICKER_PENDING:{interval}"

    # 2) The user finished picking. It is now safe to list the media items.
    items = []
    page_token = None

    while True:
        params = {"sessionId": picker_id, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            f"{PICKER_API}/mediaItems",
            headers=google_headers(),
            params=params,
            timeout=30,
        )

        if response.status_code == 401:
            return None, "GOOGLE_AUTH_EXPIRED"
        if response.status_code in (403, 404):
            return None, "PICKER_SESSION_EXPIRED"
        if response.status_code == 400 and "FAILED_PRECONDITION" in response.text:
            return None, "PICKER_PENDING:3s"
        if not response.ok:
            return None, f"PICKER_MEDIA_ERROR HTTP {response.status_code}: {response.text}"

        data = response.json()
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return items, None


def google_photo_to_data_url(item, size=1024):
    token = session.get("google_access_token")

    media_file = item.get(
        "mediaFile",
        {}
    )

    base_url = media_file.get("baseUrl")

    if not base_url:
        raise RuntimeError(
            "Google photo baseUrl missing."
        )

    image_url = (
        base_url
        + f"=w{size}-h{size}"
    )

    response = requests.get(
        image_url,
        headers={
            "Authorization": f"Bearer {token}"
        },
        timeout=45,
    )

    if not response.ok:
        raise RuntimeError(
            "Google image download failed. "
            f"HTTP {response.status_code}"
        )

    content_type = response.headers.get(
        "Content-Type",
        media_file.get(
            "mimeType",
            "image/jpeg"
        )
    )

    encoded = base64.b64encode(
        response.content
    ).decode("utf-8")

    return (
        f"data:{content_type};"
        f"base64,{encoded}"
    )




def _representative_photo_indices(items, max_count=8):
    """Choose a stable, representative subset for fast AI analysis.

    Always includes the chosen main photo when available, first/last photos and
    evenly spaced views. All original 1-24 photos remain available for eBay and
    the separate size/weight estimator.
    """
    n = len(items or [])
    if n <= 0:
        return []
    if n <= max_count:
        return list(range(1, n + 1))
    chosen = set()
    try:
        main = int(session.get("main_photo_index") or 0)
    except Exception:
        main = 0
    if 1 <= main <= n:
        chosen.add(main)
    chosen.update((1, n))
    slots = max(1, max_count - len(chosen))
    if slots == 1:
        chosen.add((n + 1) // 2)
    else:
        for i in range(slots):
            idx = 1 + round(i * (n - 1) / max(1, slots - 1))
            chosen.add(max(1, min(n, idx)))
    # Fill any gap deterministically.
    for i in range(1, n + 1):
        if len(chosen) >= max_count:
            break
        chosen.add(i)
    return sorted(chosen)[:max_count]


def _extract_json_object(text):
    """Tolerant extraction of one JSON object from a model response."""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _auto_facts_from_analysis(analysis):
    """Read the compact machine-readable facts appended by Step 1."""
    text = str(analysis or "")
    m = re.search(r"AUTO_FACTS_JSON\s*:\s*(\{.*?\})\s*$", text, re.S | re.I)
    obj = _extract_json_object(m.group(1)) if m else None
    if not isinstance(obj, dict):
        return {"materials": [], "tested_status": "Untested", "completeness": "Unknown", "brand": "", "origin": "", "confidence": 0}
    mats = obj.get("materials") or []
    if isinstance(mats, str):
        mats = [x.strip() for x in re.split(r"[,;/]", mats) if x.strip()]
    mats = [str(x).strip() for x in mats if str(x).strip()]
    status = str(obj.get("tested_status") or "Untested").strip()
    if status not in ("Untested", "Tested working", "Tested not working", "Not applicable"):
        status = "Untested"
    comp = str(obj.get("completeness") or "Unknown").strip()
    if comp not in ("Unknown", "Complete", "Incomplete"):
        comp = "Unknown"
    try:
        conf = max(0, min(100, int(float(obj.get("confidence") or 0))))
    except Exception:
        conf = 0
    return {
        "materials": list(dict.fromkeys(mats)),
        "tested_status": status,
        "completeness": comp,
        "brand": str(obj.get("brand") or "").strip(),
        "origin": str(obj.get("origin") or "").strip(),
        "confidence": conf,
    }


def _market_bin_price(market_text):
    text = str(market_text or "")
    patterns = [
        r"Normal\s+Buy\s+It\s+Now\s*[:\-]\s*\$?([0-9]+(?:\.[0-9]{1,2})?)",
        r"Recommended\s+(?:BIN|Buy\s+It\s+Now)\s*[:\-]\s*\$?([0-9]+(?:\.[0-9]{1,2})?)",
        r"Нормальн(?:а|ий).*?\$\s*([0-9]+(?:\.[0-9]{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            return m.group(1)
    return ""


def _market_title(market_text):
    text = str(market_text or "")
    pats = [
        r"(?:RECOMMENDED|РЕКОМЕНДОВАНИЙ)\s+EBAY\s+TITLE\s*[:\-]?\s*\n?([^\n]+)",
        r"(?:TITLE|ЗАГОЛОВОК)\s*[:\-]\s*([^\n]+)",
    ]
    for pat in pats:
        m = re.search(pat, text, re.I)
        if m:
            candidate = re.sub(r"^[\-*\s]+", "", m.group(1)).strip().strip('"')
            if candidate:
                return candidate[:80]
    return ""


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    try:
        _sync_current_lot_once()
    except Exception:
        pass
    google_connected = bool(
        session.get("google_access_token")
    )

    if google_connected:
        picker_id = session.get("picker_session_id")
        cached_items = PHOTO_CACHE.get(picker_id, []) if picker_id else []
        if cached_items:
            google_block = f"""
            <p style="color:green;"><b>Google Photos connected ✓</b></p>
            <p style="color:green;"><b>LOT 002 is still active — {len(cached_items)} photo(s) preserved.</b></p>
            <p><a href="/picker/items"><button style="font-size:20px;padding:12px 18px;">Continue LOT 002</button></a></p>
            <p><a href="/picker/start?new=1">Start with new photo selection</a></p>
            """
        else:
            google_block = """
            <p style="color:green;"><b>Google Photos connected ✓</b></p>
            <p><a href="/picker/start"><button style="font-size:20px;padding:12px 18px;">Start LOT 002</button></a></p>
            """
    else:
        google_block = """
        <p>
            <a href="/google/login">
                <button style="
                    font-size:20px;
                    padding:12px 18px;
                ">
                    Connect Google Photos
                </button>
            </a>
        </p>
        """

    if OPENAI_API_KEY:
        openai_block = """
        <p style="color:green;">
            <b>OpenAI API connected ✓</b>
        </p>
        """
    else:
        openai_block = """
        <p style="color:red;">
            <b>OpenAI API key missing</b>
        </p>
        """

    body = f"""
    <h2>Vintage UA eBay</h2>

    <p>Server is running ✓</p>

    {google_block}

    {openai_block}

    <hr>

    <h3>LOT workflow</h3>

    <p>
        1–24 photos →
        visual AI analysis →
        visual + web identification →
        eBay market research →
        shipping →
        final eBay listing
    </p>
    """

    return page(
        "Vintage UA eBay",
        body
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "google_configured": bool(
            GOOGLE_CLIENT_ID
            and GOOGLE_CLIENT_SECRET
        ),
        "openai_configured": bool(
            OPENAI_API_KEY
        ),
    }


# =========================================================
# GOOGLE OAUTH
# =========================================================

@app.route("/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return (
            "GOOGLE_CLIENT_ID missing",
            500,
        )

    state = secrets.token_urlsafe(32)

    session["google_oauth_state"] = state

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": PICKER_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }

    prepared = requests.Request(
        "GET",
        GOOGLE_AUTH_URL,
        params=params,
    ).prepare()

    return redirect(prepared.url)


@app.route("/google/callback")
def google_callback():
    if request.args.get("error"):
        return (
            "Google authorization error: "
            + html.escape(
                request.args.get("error")
            ),
            400,
        )

    received_state = request.args.get("state")

    saved_state = session.pop(
        "google_oauth_state",
        None,
    )

    if (
        not saved_state
        or received_state != saved_state
    ):
        return (
            "Invalid OAuth state.",
            400,
        )

    code = request.args.get("code")

    if not code:
        return (
            "Authorization code missing.",
            400,
        )

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )

    if not response.ok:
        return (
            "Google token exchange failed."
            "<br><pre>"
            + html.escape(response.text)
            + "</pre>",
            500,
        )

    token_data = response.json()

    session["google_access_token"] = (
        token_data["access_token"]
    )

    if token_data.get("refresh_token"):
        session["google_refresh_token"] = (
            token_data["refresh_token"]
        )

    return redirect("/")


# =========================================================
# PICKER START
# =========================================================

@app.route("/picker/start")
def picker_start():
    try:
        _sync_current_lot_once()
    except Exception:
        pass
    if not session.get("google_access_token"):
        return redirect("/google/login")

    # Never throw away a live LOT just because the user returned Home.
    # Reuse the already downloaded photo list; create a new Picker session only
    # when there is no cached lot or the user explicitly asks for new selection.
    current_picker = session.get("picker_session_id")
    if request.args.get("new") != "1" and current_picker and PHOTO_CACHE.get(current_picker):
        return redirect("/picker/items")

    response = requests.post(
        f"{PICKER_API}/sessions",
        headers={
            **google_headers(),
            "Content-Type": "application/json",
        },
        json={"pickingConfig": {"maxItemCount": "24"}},
        timeout=30,
    )

    if response.status_code == 401:
        session.pop(
            "google_access_token",
            None,
        )

        return redirect(
            "/google/login"
        )

    if not response.ok:
        return (
            "Could not create Picker session."
            "<br><pre>"
            + html.escape(response.text)
            + "</pre>",
            500,
        )

    data = response.json()

    picker_id = data.get("id")
    picker_uri = data.get("pickerUri")
    if picker_uri and not picker_uri.rstrip("/").endswith("autoclose"):
        picker_uri = picker_uri.rstrip("/") + "/autoclose"

    if (
        not picker_id
        or not picker_uri
    ):
        return (
            "Google Picker session data missing.",
            500,
        )

    session["picker_session_id"] = (
        picker_id
    )
    session["picker_uri"] = picker_uri
    # A genuinely new Picker session means a new lot/photo selection.
    session["photo_editor_approved"] = False
    session.pop("photo_selection_signature", None)
    session.pop("main_photo_index", None)
    session.pop("photo_order", None)
    for cache in (ANALYSIS_CACHE, IDENTIFICATION_CACHE, MARKET_CACHE, AUTO_FACT_CACHE):
        cache.pop(picker_id, None)

    body = f"""
    <h2>LOT 002</h2>

    <h3>Step 1</h3>

    <p>
        Select from <b>1 to 24 photos</b> of ONE item.
    </p>

    <p>
        <a
            href="{html.escape(picker_uri)}"
            target="_blank"
        >
            <button style="
                font-size:20px;
                padding:12px 18px;
            ">
                Open Google Photos
            </button>
        </a>
    </p>

    <hr>

    <h3>Step 2</h3>

    <p>
        After selection is complete,
        return here.
    </p>

    <p>
        <a href="/picker/items">
            <button style="
                font-size:20px;
                padding:12px 18px;
            ">
                Continue
            </button>
        </a>
    </p>
    """

    return page(
        "LOT 002",
        body
    )


# =========================================================
# PHOTO PAGE
# =========================================================

@app.route("/picker/items")
def picker_items():
    if not session.get(
        "google_access_token"
    ):
        return redirect(
            "/google/login"
        )

    items, error = get_selected_items()

    if error:
        # FAILED_PRECONDITION before mediaItemsSet=true is normal: the user is
        # still selecting or Google has not finalized the selection yet.
        if error.startswith("PICKER_PENDING"):
            # Google may need several seconds after the user taps Done. Poll briefly
            # so the seller is not trapped in a manual refresh loop.
            for _ in range(5):
                time.sleep(2)
                items2, error2 = get_selected_items()
                if not error2:
                    items, error = items2, None
                    break
                if not str(error2).startswith("PICKER_PENDING"):
                    error = error2
                    break
            if error and str(error).startswith("PICKER_PENDING"):
                picker_uri = session.get("picker_uri") or ""
                reopen = (f'<p><a href="{html.escape(picker_uri, quote=True)}" target="_blank"><button style="font-size:18px;padding:11px 16px;">Open Google Photos again</button></a></p>' if picker_uri else '')
                return page(
                    "LOT 002 — waiting for photos",
                    f"""
                    <meta http-equiv="refresh" content="3;url=/picker/items">
                    <h2>LOT 002 — waiting for Google Photos</h2>
                    <p><b>The app is checking automatically every 3 seconds.</b></p>
                    <p>If Google Photos is still open, tap <b>Done</b> there. You do not need to keep pressing Continue here.</p>
                    {reopen}
                    <p><a href="/picker/start?new=1">Cancel this selection and start a new one</a></p>
                    """
                ), 202
            # if polling finished successfully, continue below with the selected items
            if not error:
                pass
        if error == "GOOGLE_AUTH_EXPIRED":
            session.pop("google_access_token", None)
            return redirect("/google/login")
        if error in ("PICKER_SESSION_EXPIRED", "NO_PICKER_SESSION"):
            session.pop("picker_session_id", None)
            return page(
                "Photo selection expired",
                """
                <h2>LOT 002 — photo selection expired</h2>
                <p>The temporary Google Photos session is no longer available. LOT 001 is not affected.</p>
                <p><a href="/picker/start?new=1"><button style="font-size:20px;padding:12px 18px;">Select LOT 002 photos again</button></a></p>
                """
            ), 409
        if error:
            return page(
                "Google Photos error",
                "<h2>Could not load selected photos</h2><pre>" + html.escape(str(error)) + "</pre>"
                '<p><a href="/picker/items">Try again</a></p>'
            ), 502

    if not items:
        return page(
            "No photos",
            """
            <h2>No photos yet</h2>

            <p>
                Google returned zero photos.
            </p>

            <a href="/picker/items">
                Check again
            </a>
            """
        )

    picker_id = session.get(
        "picker_session_id"
    )

    # Google Photos may refresh mediaFile.baseUrl every time /mediaItems is read.
    # baseUrl is therefore NOT part of the identity of a selected photo.
    # Only reset editor approval when the actual selected item IDs/filenames change.
    def _selection_signature(seq):
        stable = []
        for obj in seq or []:
            mf = obj.get("mediaFile") or {}
            stable.append((
                str(obj.get("id") or ""),
                str(mf.get("filename") or ""),
            ))
        return tuple(stable)

    current_signature = _selection_signature(items)
    previous_signature = session.get("photo_selection_signature")
    PHOTO_CACHE[picker_id] = items

    if previous_signature is None:
        session["photo_selection_signature"] = list(current_signature)
    else:
        previous_signature = tuple(tuple(x) for x in previous_signature)
        if previous_signature != current_signature:
            session["photo_editor_approved"] = False
            session.pop("main_photo_index", None)
            session.pop("photo_order", None)
            session["photo_selection_signature"] = list(current_signature)
            for cache in (ANALYSIS_CACHE, IDENTIFICATION_CACHE, MARKET_CACHE, AUTO_FACT_CACHE):
                cache.pop(picker_id, None)
            for key in list(PHOTO_EDIT_CACHE):
                if key[0] == picker_id:
                    PHOTO_EDIT_CACHE.pop(key, None)

    count = len(items)

    cards = ""

    for index, item in enumerate(
        items,
        start=1,
    ):
        filename = html.escape(
            item.get(
                "mediaFile",
                {}
            ).get(
                "filename",
                f"Photo {index}"
            )
        )

        cards += f"""
        <div style="
            border:1px solid #ccc;
            border-radius:7px;
            padding:6px;
            text-align:center;
        ">
            <img
                src="/picker/{'edited' if session.get('photo_editor_approved') else 'thumb'}/{index}"
                loading="lazy"
                style="
                    width:100%;
                    height:160px;
                    object-fit:contain;
                "
            >

            <div style="
                font-size:11px;
                margin-top:5px;
            ">
                {index}. {filename}
            </div>
        </div>
        """

    if 1 <= count <= 24:
        status = f"""
        <p style="color:green;">
            <b>
                ✓ Correct — {count} photo(s) selected (maximum 24)
            </b>
        </p>
        """

        if session.get("photo_editor_approved"):
            button = """
            <p style="color:green"><b>✓ All selected photos approved after white-background preparation.</b></p>
            <p><a href="/picker/editor" style="display:inline-block;padding:12px 16px;border:1px solid #777;border-radius:7px;text-decoration:none">Review edited photos again</a></p>
            <form action="/picker/analyze" method="post">
                <button type="submit" style="font-size:20px;padding:14px 20px;margin-top:20px">Analyze LOT 002 with AI</button>
            </form>
            """
        else:
            button = """
            <p><b>Required before analysis/eBay:</b> prepare and approve the white-background versions.</p>
            <p><a href="/picker/editor" style="display:inline-block;padding:12px 16px;border:1px solid #777;border-radius:7px;text-decoration:none">Prepare all photos — White Background / Preview</a></p>
            """
    else:
        status = f"""
        <p style="color:red;">
            <b>
                Select from 1 to 24 photos.
                Selected: {count}
            </b>
        </p>
        """

        button = ""

    body = f"""
    <h2>LOT 002</h2>

    <h3>
        Photos: {count}/24
    </h3>

    {status}

    <div style="
        display:grid;
        grid-template-columns:
            repeat(
                auto-fill,
                minmax(135px,1fr)
            );
        gap:10px;
    ">
        {cards}
    </div>

    {button}

    <p style="margin-top:25px;">
        <a href="/">
            Return home
        </a>
    </p>
    """

    return page(
        "LOT 002 Photos",
        body
    )


# =========================================================
# THUMBNAIL
# =========================================================

@app.route(
    "/picker/thumb/<int:index>"
)
def picker_thumb(index):
    picker_id = session.get(
        "picker_session_id"
    )

    token = session.get(
        "google_access_token"
    )

    items = PHOTO_CACHE.get(
        picker_id,
        []
    )

    if (
        not token
        or not items
    ):
        return (
            "Photo cache expired",
            404,
        )

    if (
        index < 1
        or index > len(items)
    ):
        return (
            "Photo not found",
            404,
        )

    item = items[
        index - 1
    ]

    base_url = item.get(
        "mediaFile",
        {}
    ).get(
        "baseUrl"
    )

    if not base_url:
        return (
            "baseUrl missing",
            404,
        )

    response = requests.get(
        base_url + "=w600-h600",
        headers={
            "Authorization":
                f"Bearer {token}"
        },
        timeout=30,
    )

    if not response.ok:
        return (
            "Image download failed",
            response.status_code,
        )

    return Response(
        response.content,
        content_type=response.headers.get(
            "Content-Type",
            "image/jpeg"
        )
    )


# =========================================================
# STEP 1 — VISUAL ANALYSIS
# =========================================================

@app.route(
    "/picker/analyze",
    methods=["POST"],
)
def picker_analyze():
    picker_id = session.get("picker_session_id")
    items = PHOTO_CACHE.get(picker_id, [])
    if not (1 <= len(items) <= 24):
        return page("Photos expired", "<h2>Photos expired</h2><p>Select from 1 to 24 photos again.</p><a href='/picker/start'>Start again</a>"), 400
    if not session.get("photo_editor_approved"):
        return page("Photo preparation required", "<h2>Photo preparation is required first.</h2><p><a href='/picker/editor'>Open Photo Editor</a></p>"), 409
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY missing", 500

    label = _current_lot_label()
    count = len(items)
    analysis = ANALYSIS_CACHE.get(picker_id)
    used_indices = _representative_photo_indices(items, max_count=10)
    if not analysis:
        prompt = f"""You are analysing ONE vintage/collectible item for an eBay.com seller.
There are {count} seller photos in total. For speed you receive {len(used_indices)} representative original photos; do not claim that unseen details were inspected.
Do not invent manufacturer, country, age, model, material, completeness, or working status.
Separate directly visible facts from likely conclusions and unknowns. A quality/certification/state mark is not automatically a maker logo.
Focus on: item type, visible markings/text, likely materials, condition, defects, completeness, tested/untested status, and facts that matter for an eBay listing.
Return IN UKRAINIAN and keep it concise.
Use these sections:
1. ЩО ЦЕ
2. МАРКУВАННЯ / БРЕНД
3. КРАЇНА / ПЕРІОД
4. МАТЕРІАЛИ
5. КОМПЛЕКТНІСТЬ / ПРАЦЕЗДАТНІСТЬ
6. СТАН І ДЕФЕКТИ
7. НЕПІДТВЕРДЖЕНЕ
8. ВПЕВНЕНІСТЬ
At the very end output EXACTLY one machine-readable line:
AUTO_FACTS_JSON: {{"materials":[],"tested_status":"Untested","completeness":"Unknown","brand":"","origin":"","confidence":0}}
Fill materials only when visually supported. Use only tested_status values: Untested, Tested working, Tested not working, Not applicable. Use only completeness values: Unknown, Complete, Incomplete. brand/origin must be blank unless directly supported by a visible mark. confidence is 0-100.
Do not research market prices yet. Current lot: {label}."""
        content=[{"type":"input_text","text":prompt}]
        try:
            for i in used_indices:
                content.append({"type":"input_image","image_url":google_photo_to_data_url(items[i-1],640),"detail":"low"})
            analysis=call_openai({"model":"gpt-5.6-luna","input":[{"role":"user","content":content}],"reasoning":{"effort":"low"},"max_output_tokens":1100,"store":False},timeout=150)
            ANALYSIS_CACHE[picker_id]=analysis
            AUTO_FACT_CACHE[picker_id]=_auto_facts_from_analysis(analysis)
        except Exception as exc:
            return page("AI Error",f"<h2>AI analysis error</h2><pre style='white-space:pre-wrap'>{html.escape(str(exc))}</pre><a href='/picker/items'>Back</a>"),500

    facts=AUTO_FACT_CACHE.get(picker_id) or _auto_facts_from_analysis(analysis)
    mats=', '.join(facts.get('materials') or []) or 'not confidently determined'
    compact=f"<p><b>Photos:</b> {count} total; {len(used_indices)} representative photos used for this fast pass.<br><b>Auto material:</b> {html.escape(mats)}<br><b>Auto facts confidence:</b> {facts.get('confidence',0)}%</p>"
    body=f"""<h2>{label} — AI analysis</h2><p style='color:green'><b>✓ Fast visual analysis completed</b></p>{compact}
    <details><summary><b>Show full AI report</b></summary><div style='border:1px solid #ccc;padding:14px;border-radius:8px;background:#fafafa;margin-top:10px'>{text_to_html(analysis)}</div></details>
    <form action='/picker/research-identification' method='post' style='margin-top:25px'>{hidden_field('analysis',analysis)}<button type='submit' style='font-size:20px;padding:14px 20px'>Step 2 — Visual + Web Identification</button></form>"""
    return page(label+" AI Analysis",body)


# =========================================================
# STEP 2 — VISUAL + WEB IDENTIFICATION
# =========================================================

@app.route(
    "/picker/research-identification",
    methods=["POST"],
)
def research_identification():
    analysis=request.form.get("analysis","")
    if not analysis:
        return page("Missing analysis","<h2>AI analysis missing</h2><p>Run photo analysis again.</p>"),400
    picker_id=session.get("picker_session_id")
    items=PHOTO_CACHE.get(picker_id,[])
    if not (1 <= len(items) <= 24):
        return page("Photos unavailable","<h2>Original photos are no longer available.</h2><a href='/picker/start'>Select photos again</a>"),400
    label=_current_lot_label(); count=len(items)
    identification=IDENTIFICATION_CACHE.get(picker_id)
    used_indices=_representative_photo_indices(items,max_count=7)
    if not identification:
        prompt=f"""Identify ONE vintage/collectible item as accurately as possible for an eBay seller.
Current lot: {label}. Total seller photos: {count}. You receive {len(used_indices)} representative original photos plus the previous visual analysis below and Web Search.
PREVIOUS VISUAL ANALYSIS:
{analysis}

Rules:
- Visual evidence overrides generic web similarities.
- Never turn a hypothesis into a fact.
- Never invent maker, model, catalogue number, year, country, or material.
- Distinguish maker marks from quality/certification/inspection/state marks.
- Search competing hypotheses and prefer exact visual/mechanical/marking matches.
- If exact identification is not supported, say so plainly.
- Do not research price in this step.
Return IN UKRAINIAN, concise, with sections:
1. ЩО ЦЕ
2. ВИРОБНИК / БРЕНД
3. КРАЇНА / ПЕРІОД
4. МОДЕЛЬ / СЕРІЯ
5. МАРКУВАННЯ
6. ВІЗУАЛЬНІ + ВЕБ-ДОКАЗИ
7. ПІДТВЕРДЖЕНО
8. НЕПІДТВЕРДЖЕНО
9. ВПЕВНЕНІСТЬ 0–100%
10. ДЖЕРЕЛА / ТИПИ ДЖЕРЕЛ
Do not ask for extra photos unless a missing photo blocks safe identification for listing."""
        content=[{"type":"input_text","text":prompt}]
        try:
            for i in used_indices:
                content.append({"type":"input_image","image_url":google_photo_to_data_url(items[i-1],640),"detail":"low"})
            identification=call_openai({"model":"gpt-5.6-terra","tools":[{"type":"web_search"}],"input":[{"role":"user","content":content}],"reasoning":{"effort":"low"},"max_output_tokens":1500,"store":False},timeout=180)
            IDENTIFICATION_CACHE[picker_id]=identification
        except Exception as exc:
            return page("Identification Error",f"<h2>Identification research error</h2><pre style='white-space:pre-wrap'>{html.escape(str(exc))}</pre><p><a href='/picker/items'>Return to photos</a></p>"),500
    body=f"""<h2>{label} — Verified Identification</h2><p style='color:green'><b>✓ Web identification completed using {len(used_indices)} representative photos of {count}</b></p>
    <details open><summary><b>Identification result</b></summary><div style='border:1px solid #ccc;padding:14px;border-radius:8px;background:#fafafa;margin-top:10px'>{text_to_html(identification)}</div></details>
    <form action='/picker/research-market' method='post' style='margin-top:25px'>{hidden_field('analysis',analysis)}{hidden_field('identification',identification)}<button type='submit' style='font-size:20px;padding:14px 20px'>Step 3 — Research eBay Market</button></form>"""
    return page(label+" Identification",body)


def estimate_item_size_weight_from_photos():
    """AI visual estimate only. Seller must verify before shipping."""
    picker_id = session.get("picker_session_id")
    items = PHOTO_CACHE.get(picker_id, [])
    empty = {"weight_kg": "", "length_cm": "", "width_cm": "", "height_cm": "", "confidence": 0}
    if not items:
        return empty

    content = [{"type": "input_text", "text": (
        "Estimate this vintage item's physical size and approximate weight from ALL supplied photos. "
        "This is only a prefill for the seller, not a measured fact. "
        "IMPORTANT: if any photo contains a ruler, tape measure, scale, known-size object, or other visible size reference, "
        "use that reference first to estimate dimensions. Infer weight conservatively from the estimated dimensions, material appearance, and construction. "
        "Return ONLY one JSON object with numeric fields: weight_kg, length_cm, width_cm, height_cm, confidence. "
        "All four measurement fields must be positive numbers when a reasonable estimate is possible. confidence must be 0-100. No markdown, no commentary."
    )}]

    image_count = 0
    for item in items:
        try:
            content.append({"type": "input_image", "image_url": google_photo_to_data_url(item, 1024), "detail": "high"})
            image_count += 1
        except Exception:
            pass
    if image_count == 0:
        return empty

    def _parse_estimate(raw):
        text = str(raw or "").strip()
        # Accept plain JSON as requested, but tolerate fenced/extra text from the model.
        m = re.search(r'\{[\s\S]*?\}', text)
        if not m:
            return None
        data = json.loads(m.group(0))
        out = {}
        for k in ("weight_kg", "length_cm", "width_cm", "height_cm"):
            v = float(data.get(k, 0) or 0)
            if v <= 0:
                return None
            out[k] = round(v, 2)
        conf = int(round(float(data.get("confidence", 0) or 0)))
        out["confidence"] = max(1, min(100, conf))
        return out

    # First try the fast model. If it returns malformed/empty data, retry once
    # with the stronger model. This changes only the size/weight prefill step.
    for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
        try:
            raw = call_openai({
                "model": model,
                "input": [{"role": "user", "content": content}],
                "reasoning": {"effort": "low"},
                "max_output_tokens": 300,
                "store": False,
            }, timeout=270)
            parsed = _parse_estimate(raw)
            if parsed:
                return parsed
        except Exception:
            continue

    return empty

# =========================================================
# STEP 3 — EBAY MARKET
# =========================================================

@app.route(
    "/picker/research-market",
    methods=["POST"],
)
def research_market():
    analysis=request.form.get("analysis",""); identification=request.form.get("identification","")
    if not analysis or not identification:
        return page("Missing research data","<h2>Research data missing</h2><p>Return to previous step.</p>"),400
    picker_id=session.get("picker_session_id"); label=_current_lot_label()
    market=MARKET_CACHE.get(picker_id)
    if not market:
        prompt=f"""Perform concise eBay.com market research for one vintage/collectible item.
Current lot: {label}.
VISUAL ANALYSIS:
{analysis}

VERIFIED IDENTIFICATION:
{identification}

Use the verified identification as primary search basis. Use Web Search.
Rules:
- Strictly separate CONFIRMED SOLD/COMPLETED eBay evidence from ACTIVE asking prices.
- Never call an active listing sold.
- If sold status cannot be independently confirmed, say so.
- Prefer exact/near-exact maker, design, mechanism, material, condition and completeness.
- If no confirmed sold comp exists, still provide a conservative recommended BIN derived from the closest active/other market comparables and explicitly label confidence low/medium.
- Do not invent item numbers, sold prices, dates, makers or model numbers.
- Account for visible defects, untested/non-working status, incompleteness and shipping from Ukraine.
Return IN UKRAINIAN and keep it concise. Use exactly:
1. CONFIRMED SOLD EBAY COMPS
2. ACTIVE / OTHER COMPARABLES
3. CLOSEST COMPARABLE
4. PRICE LOGIC
5. RECOMMENDED PRICES
Quick sale: $...
Normal Buy It Now: $...
Patient seller: $...
Minimum acceptable offer: $...
6. RECOMMENDED EBAY TITLE
7. RECOMMENDED CATEGORY / CONDITION
8. KEY ITEM SPECIFICS
9. CONFIDENCE
10. SOURCES
The four recommended prices must be numeric USD values whenever there is enough market evidence to list the item; if evidence is weak, choose conservative values and state low confidence rather than omitting the price."""
        try:
            market=call_openai({"model":"gpt-5.6-terra","tools":[{"type":"web_search"}],"reasoning":{"effort":"low"},"input":prompt,"max_output_tokens":1700,"store":False},timeout=180)
            MARKET_CACHE[picker_id]=market
        except Exception as exc:
            return page("Market Research Error",f"<h2>eBay market research error</h2><pre style='white-space:pre-wrap'>{html.escape(str(exc))}</pre><p>Use Back in the browser.</p>"),500

    visual_estimate=estimate_item_size_weight_from_photos()
    est_weight=visual_estimate.get("weight_kg",""); est_l=visual_estimate.get("length_cm",""); est_w=visual_estimate.get("width_cm",""); est_h=visual_estimate.get("height_cm",""); est_conf=visual_estimate.get("confidence",0)
    price=_market_bin_price(market)
    body=f"""<h2>{label} — Market Research</h2><p style='color:green'><b>✓ eBay market research completed</b></p>
    <p><b>Suggested normal BIN:</b> {('$'+html.escape(price)) if price else 'not parsed — you can correct it before Preflight'}</p>
    <details><summary><b>Show identification</b></summary><div style='border:1px solid #ddd;padding:12px;border-radius:8px;margin-top:8px'>{text_to_html(identification)}</div></details>
    <details open><summary><b>Show market research</b></summary><div style='border:1px solid #ccc;padding:14px;border-radius:8px;background:#fafafa;margin-top:8px'>{text_to_html(market)}</div></details>
    <hr><h3>Step 4 — Weight & Shipping</h3><p>AI pre-estimated size/weight from the photos. Correct only if you know a value is wrong. Confidence: <b>{est_conf}%</b>.</p>
    <form action='/picker/shipping' method='post'>{hidden_field('analysis',analysis)}{hidden_field('identification',identification)}{hidden_field('market',market)}
    <p><label><b>Item weight without packaging (kg):</b><br><input type='number' name='item_weight' step='0.01' min='0.01' value='{est_weight}' required style='font-size:20px;padding:10px;width:180px'></label></p>
    <p><label><b>Estimated length, cm:</b><br><input type='number' name='dim_l' step='0.1' min='0.1' value='{est_l}' required style='font-size:20px;padding:10px;width:180px'></label></p>
    <p><label><b>Estimated width, cm:</b><br><input type='number' name='dim_w' step='0.1' min='0.1' value='{est_w}' required style='font-size:20px;padding:10px;width:180px'></label></p>
    <p><label><b>Estimated height, cm:</b><br><input type='number' name='dim_h' step='0.1' min='0.1' value='{est_h}' required style='font-size:20px;padding:10px;width:180px'></label></p>
    <button type='submit' style='font-size:20px;padding:14px 20px'>Step 4 — Weight & Shipping</button></form>"""
    return page(label+" Market Research",body)

# ============================================================
# STEP 4 — WEIGHT & SHIPPING
# ============================================================
# ============================================================
# STEP 4 — WEIGHT & SHIPPING
# ============================================================

@app.route("/picker/shipping", methods=["POST"])
def shipping():
    analysis=request.form.get("analysis",""); identification=request.form.get("identification",""); market=request.form.get("market","")
    try: item_weight=float(request.form.get("item_weight","").strip().replace(",","."))
    except (TypeError,ValueError): return page("Invalid weight","<h2>Enter item weight in kg.</h2>"),400
    if not analysis or not identification or not market or item_weight<=0: return page("Missing LOT data","<h2>LOT data missing or invalid.</h2>"),400
    if item_weight<=0.5: allowance=0.30
    elif item_weight<=1: allowance=0.45
    elif item_weight<=2: allowance=0.65
    elif item_weight<=3: allowance=0.85
    elif item_weight<=5: allowance=1.10
    elif item_weight<=10: allowance=1.60
    else: allowance=max(2.0,item_weight*0.18)
    packed=round(item_weight+allowance,2)
    dl=request.form.get("dim_l","").strip(); dw=request.form.get("dim_w","").strip(); dh=request.form.get("dim_h","").strip()
    try:
        L,W,H=[float(x.replace(",",".")) for x in (dl,dw,dh)]
        if min(L,W,H)<=0: raise ValueError
    except Exception: return page("Invalid dimensions","<h2>Use positive dimensions.</h2>"),400
    pL,pW,pH=round(L+10,1),round(W+10,1),round(H+10,1)
    postage=9.0+9.0*packed
    if max(pL,pW,pH)>70: postage*=1.25
    postage=round(postage,2); reserve=min(7.0,max(3.0,postage*0.07)); ebay_ship=float(int(postage+reserve))+0.99
    picker_id=session.get("picker_session_id"); count=len(PHOTO_CACHE.get(picker_id,[])); label=_current_lot_label()
    facts=AUTO_FACT_CACHE.get(picker_id) or _auto_facts_from_analysis(analysis)
    auto_mats=facts.get("materials") or []
    choices=["Plastic","Metal","Wood","Glass","Ceramic","Fabric/Textile","Paper/Cardboard","Rubber","Leather","String/Cord"]
    checks=[]
    for c in choices:
        selected=any(c.casefold()==str(x).casefold() for x in auto_mats)
        checks.append(f"<label><input type='checkbox' name='material_choice' value='{html.escape(c,quote=True)}' {'checked' if selected else ''}> {html.escape(c)}</label>")
    status=facts.get('tested_status') or 'Untested'; comp=facts.get('completeness') or 'Unknown'
    status_options=''.join(f"<option value='{html.escape(x,quote=True)}' {'selected' if x==status else ''}>{html.escape(x)}</option>" for x in ("Untested","Tested working","Tested not working","Not applicable"))
    comp_options=''.join(f"<option value='{x}' {'selected' if x==comp else ''}>{x}</option>" for x in ("Unknown","Complete","Incomplete"))
    body=f"""<h2>{label} — Weight & Shipping</h2><p>✓ Photos: <b>{count}</b> / 24<br>✓ Item weight: <b>{item_weight:.2f} kg</b><br>✓ Estimated packed weight: <b>{packed:.2f} kg</b><br>✓ Item dimensions: <b>{L:g} × {W:g} × {H:g} cm</b><br>✓ Estimated package: <b>{pL:g} × {pW:g} × {pH:g} cm</b><br>✓ Postage estimate: <b>${postage:.2f}</b><br>✓ Recommended eBay shipping: <b>${ebay_ship:.2f}</b></p>
    <form action='/picker/final-listing' method='post'>{hidden_field('analysis',analysis)}{hidden_field('identification',identification)}{hidden_field('market',market)}{hidden_field('item_weight',str(item_weight))}{hidden_field('estimated_packed_weight',str(packed))}{hidden_field('dimensions',f'{L:g} x {W:g} x {H:g} cm')}{hidden_field('package_dimensions',f'{pL:g} x {pW:g} x {pH:g} cm')}{hidden_field('shipping_usd',f'{ebay_ship:.2f}')}
    <fieldset style='padding:12px;margin:14px 0'><legend><b>AUTO FACTS — correct only if needed</b></legend><p>AI confidence: <b>{facts.get('confidence',0)}%</b>. Known values are prefilled automatically.</p><p><b>Material(s):</b><br>{' &nbsp; '.join(checks)}</p><p>Other material:<br><input name='material_other' value='' placeholder='optional' style='font-size:17px;padding:8px;width:min(420px,90%)'></p>
    <p><b>Working / tested status:</b><br><select name='seller_tested_status' style='font-size:17px;padding:8px'>{status_options}</select></p>
    <p><b>Completeness:</b><br><select name='seller_completeness' style='font-size:17px;padding:8px'>{comp_options}</select></p>
    <p><b>Brand / maker:</b><br><input name='seller_brand' value='{html.escape(facts.get('brand') or '',quote=True)}' placeholder='blank if not confirmed' style='font-size:17px;padding:8px;width:min(420px,90%)'></p>
    <p><b>Country of origin:</b><br><input name='seller_origin' value='{html.escape(facts.get('origin') or '',quote=True)}' placeholder='blank if not confirmed' style='font-size:17px;padding:8px;width:min(420px,90%)'></p><p><small>You do not need to re-enter known data. Change only something you know is wrong.</small></p></fieldset>
    <button type='submit' style='font-size:20px;padding:14px 20px'>Create Final eBay Listing Draft</button></form>"""
    return page(label+" Weight & Shipping",body)

@app.route("/picker/final-listing", methods=["POST"])
def final_listing():
    analysis=request.form.get("analysis",""); identification=request.form.get("identification",""); market=request.form.get("market",""); item_weight=request.form.get("item_weight",""); packed_weight=request.form.get("estimated_packed_weight",""); dimensions=request.form.get("dimensions",""); package_dimensions=request.form.get("package_dimensions",""); shipping_usd=request.form.get("shipping_usd","")
    materials=[x.strip() for x in request.form.getlist("material_choice") if x.strip()]; other=request.form.get("material_other","").strip()
    if other: materials.append(other)
    materials=list(dict.fromkeys(materials)); confirmed_material=", ".join(materials)
    seller_tested_status=request.form.get("seller_tested_status","Untested").strip() or "Untested"; seller_completeness=request.form.get("seller_completeness","Unknown").strip() or "Unknown"; seller_brand=request.form.get("seller_brand","").strip(); seller_origin=request.form.get("seller_origin","").strip()
    label=_current_lot_label(); fallback_price=_market_bin_price(market); fallback_title=_market_title(market)
    prompt=f"""Create one complete eBay.com listing draft for {label}. Return ONLY a JSON object, no markdown.
Source evidence:
VISUAL ANALYSIS:
{analysis}

VERIFIED IDENTIFICATION:
{identification}

MARKET RESEARCH:
{market}

Seller/automatic facts: materials={confirmed_material or 'Unknown'}; tested_status={seller_tested_status}; completeness={seller_completeness}; brand={seller_brand or 'not confirmed'}; origin={seller_origin or 'not confirmed'}.
Measurements: item_weight={item_weight} kg; packed_weight={packed_weight} kg; item_dimensions={dimensions}; package_dimensions={package_dimensions}; buyer_shipping=${shipping_usd} from Ukraine to USA.
Rules: seller/automatic facts above override conflicting web guesses. If materials are Unknown, do not invent them. Never claim unconfirmed maker/model/origin/year. Preserve all visible defects and tested status. Title max 80 characters, strong but truthful SEO. Description 180-350 words. SEO 12-24 truthful phrases. Price should equal the Normal Buy It Now market recommendation; if market confidence is low, still use its conservative normal BIN. Do not include shipping in item price.
JSON schema exactly: {{"title":"","price_usd":"","condition_note":"","item_specifics":{{}},"description":"","seo_keywords":[],"seller_check":[]}}"""
    try:
        raw=call_openai({"model":"gpt-5.6-terra","input":prompt,"reasoning":{"effort":"low"},"max_output_tokens":1800,"store":False},timeout=180)
        data=_extract_json_object(raw) or {}
    except Exception as exc:
        return page(label+" draft error",f"<h2>Final draft generation failed</h2><pre style='white-space:pre-wrap'>{html.escape(str(exc))}</pre><p>Nothing was sent to eBay.</p>"),500
    title=str(data.get("title") or fallback_title or "").strip()[:80]
    price=str(data.get("price_usd") or fallback_price or "").strip().replace("$","").replace(",","")
    pm=re.search(r"([0-9]+(?:\.[0-9]{1,2})?)",price); price=pm.group(1) if pm else ""
    condition_note=str(data.get("condition_note") or "").strip()[:1000]
    specifics=data.get("item_specifics") if isinstance(data.get("item_specifics"),dict) else {}
    description=str(data.get("description") or "").strip()
    seo=data.get("seo_keywords") if isinstance(data.get("seo_keywords"),list) else []
    checks=data.get("seller_check") if isinstance(data.get("seller_check"),list) else []
    if not title:
        return page(label+" draft stop","<h2>Safety stop — no usable title was generated.</h2><p>Nothing was sent to eBay.</p>"),409
    listing_text=(f"1 TITLE\n{title}\n\n2 PRICE USD\n${price if price else 'Not set'}\n\n3 CONDITION\n{condition_note}\n\n4 ITEM SPECIFICS\n"+"\n".join(f"{k}: {v}" for k,v in specifics.items())+f"\n\n5 DESCRIPTION\n{description}\n\n6 SEO KEYWORDS\n"+", ".join(str(x) for x in seo)+"\n\n7 SELLER CHECK\n"+"; ".join(str(x) for x in checks))
    draft={"listing_text":listing_text,"title":title,"price":price,"condition_note":condition_note,"description":description,"item_specifics":specifics,"shipping_usd":shipping_usd,"confirmed_material":confirmed_material,"seller_tested_status":seller_tested_status,"seller_completeness":seller_completeness,"seller_brand":seller_brand,"seller_origin":seller_origin,"item_weight":item_weight,"packed_weight":packed_weight,"dimensions":dimensions,"package_dimensions":package_dimensions}
    LOT002_DRAFT_CACHE[session.get("picker_session_id")]=draft
    price_note="" if price else "<p style='color:#8a4b00'><b>Price could not be extracted automatically.</b> Enter it below; nothing goes to eBay until you continue.</p>"
    return page(label+" Final eBay Listing",f"""<h2>{label} — Final eBay Listing Draft</h2><p style='color:#176b2c'><b>Draft ready. Review or correct only what is needed.</b></p>{price_note}<pre style='white-space:pre-wrap'>{html.escape(listing_text)}</pre><hr><form action='/picker/confirm-final-draft' method='post'><p><label><b>Final Buy It Now price, USD:</b><br><input name='confirmed_price' type='number' min='0.01' step='0.01' value='{html.escape(price,quote=True)}' required placeholder='Enter price' style='font-size:20px;padding:10px;width:180px'></label></p><p><label><b>Material(s), editable:</b><br><input name='confirmed_material' value='{html.escape(confirmed_material,quote=True)}' placeholder='leave blank if unknown' style='font-size:18px;padding:10px;width:min(520px,90%)'></label></p><button type='submit' style='font-size:18px;padding:14px 18px;font-weight:bold'>Use these values → eBay Preflight</button></form>""")

@app.route("/picker/confirm-final-draft", methods=["POST"])
def confirm_final_draft():
    draft=LOT002_DRAFT_CACHE.get(session.get("picker_session_id")) or {}; label=_current_lot_label()
    if not draft: return page("Draft missing","<h2>Draft is missing. Nothing was sent to eBay.</h2>"),400
    raw=request.form.get("confirmed_price","").strip().replace(",",".")
    try:
        val=float(raw)
        if val<=0 or val>999999: raise ValueError
    except ValueError: return page("Invalid price","<h2>Enter a valid positive USD price.</h2><p>Nothing was sent to eBay.</p>"),400
    draft["price"]=f"{val:.2f}"; draft["price_confirmed"]=True; draft["confirmed_material"]=request.form.get("confirmed_material","").strip(); draft["material_confirmed"]=True; draft["seller_reviewed"]=True
    LOT002_DRAFT_CACHE[session.get("picker_session_id")]=draft
    return redirect("/ebay/current-preflight")

# =========================================================
# EBAY MEDIA API — SAFE ONE-PHOTO TEST FOR LOT 001
# =========================================================
# =========================================================
# EBAY MEDIA API — SAFE ONE-PHOTO TEST FOR LOT 001
# =========================================================

@app.route("/ebay/test-media-photo", methods=["GET", "POST"])
def ebay_test_media_photo():
    """Upload ONLY photo #1 from the current LOT 001 Picker cache to eBay EPS."""
    picker_id = session.get("picker_session_id")
    google_token = session.get("google_access_token")
    items = PHOTO_CACHE.get(picker_id, [])

    if request.method == "GET":
        if not google_token:
            return page(
                "eBay Media API test",
                """
                <h2>eBay Media API — one-photo test</h2>
                <p><b>Google Photos session is not active.</b></p>
                <p>Open Google Photos Picker and select LOT 001 photos again before this test.</p>
                <p>No photo was uploaded to eBay.</p>
                """,
            ), 400

        if not items:
            return page(
                "eBay Media API test",
                """
                <h2>eBay Media API — one-photo test</h2>
                <p><b>Photo cache is empty.</b></p>
                <p>Open the selected-photos page first so the current Picker selection is loaded.</p>
                <p>No photo was uploaded to eBay.</p>
                """,
            ), 400

        first = items[0]
        media_file = first.get("mediaFile", {})
        filename = html.escape(media_file.get("filename", "LOT-001-photo-01.jpg"))
        return page(
            "eBay Media API test",
            f"""
            <h2>eBay Media API — one-photo test</h2>
            <p><b>LOT:</b> LOT 001</p>
            <p><b>Selected photos currently cached:</b> {len(items)}</p>
            <p><b>Test photo:</b> #1 — {filename}</p>
            <p>This test uploads <b>ONLY ONE photo</b> to eBay Picture Services (EPS).</p>
            <p>It does <b>not</b> create an Inventory Item, Offer, or live eBay listing.</p>
            <form method="post">
                <button type="submit" style="padding:12px 18px;font-size:16px">
                    Confirm and upload photo #1 to eBay EPS
                </button>
            </form>
            <p><a href="/picker/items">Cancel / return to LOT 001 photos</a></p>
            """,
        )

    if not google_token or not items:
        return page(
            "eBay Media API test",
            """
            <h2>Cannot run Media API test</h2>
            <p>The Google Photos session or temporary photo cache has expired.</p>
            <p>Select LOT 001 photos again. Nothing was uploaded to eBay.</p>
            """,
        ), 400

    first = items[0]
    media_file = first.get("mediaFile", {})
    base_url = media_file.get("baseUrl")
    filename = media_file.get("filename", "LOT-001-photo-01.jpg")
    mime_type = media_file.get("mimeType", "image/jpeg")

    if not base_url:
        return page(
            "eBay Media API test",
            "<h2>Photo #1 has no Google baseUrl.</h2><p>Nothing was uploaded to eBay.</p>",
        ), 400

    try:
        # Download a large listing-quality copy from the user's current Picker selection.
        google_response = requests.get(
            base_url + "=w2400-h2400",
            headers={"Authorization": f"Bearer {google_token}"},
            timeout=60,
        )
        if not google_response.ok:
            return page(
                "eBay Media API test",
                f"""
                <h2>Google photo download failed</h2>
                <p><b>HTTP {google_response.status_code}</b></p>
                <p>Nothing was uploaded to eBay.</p>
                """,
            ), 502

        actual_mime = google_response.headers.get("Content-Type", mime_type).split(";")[0].strip()
        if not actual_mime.startswith("image/"):
            actual_mime = mime_type if str(mime_type).startswith("image/") else "image/jpeg"

        access_token = get_ebay_access_token()
        media_response = requests.post(
            "https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            files={
                "image": (
                    filename,
                    google_response.content,
                    actual_mime,
                )
            },
            timeout=120,
        )

        if media_response.status_code != 201:
            return page(
                "eBay Media API test failed",
                f"""
                <h2>eBay Media API upload failed</h2>
                <p><b>HTTP {media_response.status_code}</b></p>
                <pre style="white-space:pre-wrap">{html.escape(media_response.text)}</pre>
                <p>No Inventory Item, Offer, or live listing was created.</p>
                """,
            ), 502

        data = media_response.json() if media_response.text.strip() else {}
        location = media_response.headers.get("Location", "")
        image_id = location.rstrip("/").split("/")[-1] if location else ""
        image_url = data.get("maxDimensionImageUrl") or data.get("imageUrl") or ""
        expiration = data.get("expirationDate", "")

        # If create response does not contain a usable EPS URL, retrieve image details.
        if image_id and not image_url:
            details_response = requests.get(
                f"https://apim.ebay.com/commerce/media/v1_beta/image/{image_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            if details_response.ok:
                details = details_response.json()
                image_url = details.get("maxDimensionImageUrl") or details.get("imageUrl") or ""
                expiration = details.get("expirationDate", expiration)

        safe_url = html.escape(str(image_url), quote=True)
        url_html = (
            f'<p><b>EPS image URL:</b><br><a href="{safe_url}" target="_blank" rel="noopener">{safe_url}</a></p>'
            if image_url else
            "<p><b>EPS image URL:</b> not returned yet; image ID was created successfully.</p>"
        )

        return page(
            "eBay Media API test successful",
            f"""
            <h2>eBay Media API test successful</h2>
            <p><b>Uploaded:</b> LOT 001 photo #1 only</p>
            <p><b>Image ID:</b> <code>{html.escape(image_id)}</code></p>
            {url_html}
            <p><b>Expiration:</b> {html.escape(str(expiration)) if expiration else 'not returned'}</p>
            <p>No Inventory Item, Offer, or live eBay listing was created.</p>
            """,
        )

    except Exception as exc:
        return page(
            "eBay Media API test error",
            f"""
            <h2>eBay Media API test error</h2>
            <pre style="white-space:pre-wrap">{html.escape(str(exc))}</pre>
            <p>No Inventory Item, Offer, or live eBay listing was created.</p>
            """,
        ), 500


# =========================================================
# OLD LINK
# =========================================================

@app.route("/picker/prepare")
def picker_prepare():
    return redirect(
        "/picker/items"
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        ),
    )

# =========================================================
# EBAY MEDIA API — SAFE 24-PHOTO LOT 001 UPLOAD
# =========================================================

EBAY_MEDIA_CACHE = {}

@app.route('/ebay/upload-lot001-photos', methods=['GET', 'POST'])
def ebay_upload_lot001_photos():
    """Upload exactly 24 cached LOT 001 photos to eBay EPS; never create/publish a listing."""
    picker_id = session.get('picker_session_id')
    google_token = session.get('google_access_token')
    items = PHOTO_CACHE.get(picker_id, [])

    if request.method == 'GET':
        if not google_token or len(items) != 24:
            return page('LOT 001 photo upload', '''
                <h2>LOT 001 — 24-photo EPS upload</h2>
                <p><b>24 active Google Photos are required.</b></p>
                <p>Select/load the 24 LOT 001 photos again before continuing.</p>
                <p>Nothing was uploaded by this page.</p>
            '''), 400

        rows = []
        for index, item in enumerate(items, start=1):
            filename = item.get('mediaFile', {}).get('filename', f'photo-{index:02d}.jpg')
            rows.append(f'<li>#{index}: {html.escape(filename)}</li>')

        return page('Confirm LOT 001 photos', f'''
            <h2>LOT 001 — confirm 24-photo EPS upload</h2>
            <p><b>Photos ready: 24 / 24</b></p>
            <p>This action uploads exactly these 24 photos to eBay Picture Services (EPS).</p>
            <p><b>It does NOT create an Inventory Item, Offer, or live eBay listing.</b></p>
            <ol>{''.join(rows)}</ol>
            <form method="post">
                <button type="submit" style="padding:12px 18px;font-size:16px">
                    Confirm and upload all 24 photos to eBay EPS
                </button>
            </form>
            <p><a href="/picker/items">Cancel / return to LOT 001 photos</a></p>
        ''')

    if not google_token or len(items) != 24:
        return page('LOT 001 photo upload', '''
            <h2>Upload stopped safely</h2>
            <p>The Google Photos session/cache is unavailable or does not contain exactly 24 photos.</p>
            <p>No Inventory Item, Offer, or live listing was created.</p>
        '''), 400

    access_token = get_ebay_access_token()
    results = []

    for index, item in enumerate(items, start=1):
        media_file = item.get('mediaFile', {})
        base_url = media_file.get('baseUrl')
        filename = media_file.get('filename', f'LOT-001-photo-{index:02d}.jpg')
        mime_type = media_file.get('mimeType', 'image/jpeg')

        if not base_url:
            return page('LOT 001 photo upload failed', f'''
                <h2>Upload stopped at photo #{index}</h2>
                <p>Google baseUrl is missing for {html.escape(filename)}.</p>
                <p><b>{len(results)} photo(s) had already reached EPS before the stop.</b></p>
                <p>No Inventory Item, Offer, or live listing was created.</p>
            '''), 502

        google_response = requests.get(
            base_url + '=w2400-h2400',
            headers={'Authorization': f'Bearer {google_token}'},
            timeout=60,
        )
        if not google_response.ok:
            return page('LOT 001 photo upload failed', f'''
                <h2>Upload stopped at photo #{index}</h2>
                <p>Google download failed: HTTP {google_response.status_code}.</p>
                <p><b>{len(results)} photo(s) had already reached EPS before the stop.</b></p>
                <p>No Inventory Item, Offer, or live listing was created.</p>
            '''), 502

        actual_mime = google_response.headers.get('Content-Type', mime_type).split(';')[0].strip()
        if not actual_mime.startswith('image/'):
            actual_mime = mime_type if str(mime_type).startswith('image/') else 'image/jpeg'

        media_response = requests.post(
            'https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file',
            headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'},
            files={'image': (filename, _safe_photo_edit(google_response.content) if session.get('photo_editor_approved') else google_response.content, 'image/jpeg' if session.get('photo_editor_approved') else actual_mime)},
            timeout=120,
        )
        if media_response.status_code != 201:
            return page('LOT 001 photo upload failed', f'''
                <h2>eBay EPS upload stopped at photo #{index}</h2>
                <p><b>HTTP {media_response.status_code}</b></p>
                <pre style="white-space:pre-wrap">{html.escape(media_response.text)}</pre>
                <p><b>{len(results)} photo(s) had already reached EPS before the stop.</b></p>
                <p>No Inventory Item, Offer, or live listing was created.</p>
            '''), 502

        data = media_response.json() if media_response.text.strip() else {}
        location = media_response.headers.get('Location', '')
        image_id = location.rstrip('/').split('/')[-1] if location else ''
        image_url = data.get('maxDimensionImageUrl') or data.get('imageUrl') or ''
        expiration = data.get('expirationDate', '')

        if image_id and not image_url:
            details_response = requests.get(
                f'https://apim.ebay.com/commerce/media/v1_beta/image/{image_id}',
                headers={'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'},
                timeout=30,
            )
            if details_response.ok:
                details = details_response.json()
                image_url = details.get('maxDimensionImageUrl') or details.get('imageUrl') or ''
                expiration = details.get('expirationDate', expiration)

        if not image_id or not image_url:
            return page('LOT 001 photo upload failed', f'''
                <h2>EPS response incomplete at photo #{index}</h2>
                <p>The upload returned success but a usable Image ID / EPS URL was not available.</p>
                <p><b>{len(results) + 1} photo(s) may already exist in EPS.</b></p>
                <p>No Inventory Item, Offer, or live listing was created.</p>
            '''), 502

        results.append({
            'number': index,
            'filename': filename,
            'image_id': image_id,
            'image_url': image_url,
            'expiration': expiration,
        })

    EBAY_MEDIA_CACHE['LOT-001'] = results
    rows = []
    for result in results:
        safe_url = html.escape(result['image_url'], quote=True)
        rows.append(
            '<tr>'
            f"<td>{result['number']}</td>"
            f"<td>{html.escape(result['filename'])}</td>"
            f"<td><code>{html.escape(result['image_id'])}</code></td>"
            f'<td><a href="{safe_url}" target="_blank" rel="noopener">EPS image</a></td>'
            '</tr>'
        )

    return page('LOT 001 EPS upload successful', f'''
        <h2>LOT 001 — all 24 photos uploaded successfully</h2>
        <p><b>EPS URLs stored in temporary Render memory: 24 / 24</b></p>
        <p>No Inventory Item, Offer, or live eBay listing was created.</p>
        <table border="1" cellpadding="5" cellspacing="0">
            <tr><th>#</th><th>File</th><th>Image ID</th><th>EPS</th></tr>
            {''.join(rows)}
        </table>
    ''')


# =========================================================
# EBAY INVENTORY API — LOT 001 SAFE CREATE
# Upload 24 photos to EPS, then create/replace Inventory Item.
# NO Offer and NO Publish are performed here.
# =========================================================

@app.route('/ebay/create-lot001-inventory', methods=['GET', 'POST'])
def ebay_create_lot001_inventory():
    sku = 'LOT-001'
    picker_id = session.get('picker_session_id')
    google_token = session.get('google_access_token')
    items = PHOTO_CACHE.get(picker_id, [])

    if request.method == 'GET':
        if not google_token or len(items) != 24:
            return page('LOT 001 Inventory Item', '''
                <h2>LOT 001 — Inventory Item preparation</h2>
                <p><b>Exactly 24 active Google Photos are required.</b></p>
                <p>Open the LOT 001 Picker, select the 24 photos, return, and press Continue first.</p>
                <p><b>Nothing was created on eBay by this page.</b></p>
            '''), 400

        rows = []
        for index, item in enumerate(items, start=1):
            filename = item.get('mediaFile', {}).get(
                'filename',
                f'LOT-001-photo-{index:02d}.jpg'
            )
            rows.append(f'<li>#{index}: {html.escape(filename)}</li>')

        return page('Confirm LOT 001 Inventory Item', f'''
            <h2>LOT 001 — confirm Inventory Item creation</h2>
            <p><b>Photos ready: 24 / 24</b></p>
            <p><b>SKU:</b> <code>{sku}</code></p>
            <p><b>Condition:</b> For parts or not working</p>
            <p><b>Quantity:</b> 1</p>
            <p><b>Packed weight:</b> 3.25 kg</p>
            <p><b>Package:</b> 45 × 39 × 22 cm</p>
            <p>
                This action uploads these 24 photos to eBay Picture Services (EPS)
                and then creates/replaces the unpublished eBay Inventory Item
                <code>{sku}</code>.
            </p>
            <p style="font-weight:bold;">
                It does NOT create an Offer and does NOT publish a live eBay listing.
            </p>
            <ol>{''.join(rows)}</ol>
            <form method="post">
                <button type="submit" style="padding:12px 18px;font-size:16px">
                    Confirm: upload 24 photos and create Inventory Item LOT-001
                </button>
            </form>
            <p><a href="/picker/items">Cancel / return to LOT 001 photos</a></p>
        ''')

    if not google_token or len(items) != 24:
        return page('LOT 001 Inventory Item stopped', '''
            <h2>LOT 001 — stopped safely</h2>
            <p>The Google Photos session/cache is unavailable or does not contain exactly 24 photos.</p>
            <p>No Inventory Item, Offer, or live listing was created by this request.</p>
        '''), 400

    try:
        access_token = get_ebay_access_token()
        eps_results = []

        for index, item in enumerate(items, start=1):
            media_file = item.get('mediaFile', {})
            base_url = media_file.get('baseUrl')
            filename = media_file.get(
                'filename',
                f'LOT-001-photo-{index:02d}.jpg'
            )
            mime_type = media_file.get('mimeType', 'image/jpeg')

            if not base_url:
                return page('LOT 001 EPS upload stopped', f'''
                    <h2>Stopped at photo #{index}</h2>
                    <p>Google baseUrl is missing for {html.escape(filename)}.</p>
                    <p><b>{len(eps_results)} photo(s) may already have reached EPS.</b></p>
                    <p>No Inventory Item, Offer, or live listing was created.</p>
                '''), 400

            google_response = requests.get(
                base_url + '=w2400-h2400',
                headers={'Authorization': f'Bearer {google_token}'},
                timeout=60,
            )

            if not google_response.ok:
                return page('LOT 001 EPS upload stopped', f'''
                    <h2>Google photo download failed at photo #{index}</h2>
                    <p><b>HTTP {google_response.status_code}</b></p>
                    <p><b>{len(eps_results)} photo(s) may already have reached EPS.</b></p>
                    <p>No Inventory Item, Offer, or live listing was created.</p>
                '''), 502

            actual_mime = google_response.headers.get(
                'Content-Type',
                mime_type
            ).split(';')[0].strip()

            if not actual_mime.startswith('image/'):
                actual_mime = (
                    mime_type if str(mime_type).startswith('image/')
                    else 'image/jpeg'
                )

            media_response = requests.post(
                'https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Accept': 'application/json',
                },
                files={
                    'image': (
                        filename,
                        _safe_photo_edit(google_response.content) if session.get('photo_editor_approved') else google_response.content,
                        'image/jpeg' if session.get('photo_editor_approved') else actual_mime,
                    )
                },
                timeout=120,
            )

            if media_response.status_code != 201:
                return page('LOT 001 EPS upload stopped', f'''
                    <h2>eBay EPS upload failed at photo #{index}</h2>
                    <p><b>HTTP {media_response.status_code}</b></p>
                    <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(media_response.text[:5000])}</pre>
                    <p><b>{len(eps_results)} photo(s) had already reached EPS before the stop.</b></p>
                    <p>No Inventory Item, Offer, or live listing was created.</p>
                '''), 502

            media_data = media_response.json() if media_response.text.strip() else {}
            location = media_response.headers.get('Location', '')
            image_id = location.rstrip('/').split('/')[-1] if location else ''
            image_url = (
                media_data.get('maxDimensionImageUrl')
                or media_data.get('imageUrl')
                or ''
            )

            if image_id and not image_url:
                details_response = requests.get(
                    f'https://apim.ebay.com/commerce/media/v1_beta/image/{image_id}',
                    headers={
                        'Authorization': f'Bearer {access_token}',
                        'Accept': 'application/json',
                    },
                    timeout=30,
                )
                if details_response.ok:
                    details = details_response.json()
                    image_url = (
                        details.get('maxDimensionImageUrl')
                        or details.get('imageUrl')
                        or ''
                    )

            if not image_url:
                return page('LOT 001 EPS upload stopped', f'''
                    <h2>No usable EPS URL for photo #{index}</h2>
                    <p><b>{len(eps_results) + 1} photo(s) may already exist in EPS.</b></p>
                    <p>No Inventory Item, Offer, or live listing was created.</p>
                '''), 502

            eps_results.append({
                'number': index,
                'filename': filename,
                'image_id': image_id,
                'image_url': image_url,
            })

        if len(eps_results) != 24:
            return page('LOT 001 Inventory Item stopped', '''
                <h2>Safety stop</h2>
                <p>Exactly 24 usable EPS URLs were not obtained.</p>
                <p>No Inventory Item, Offer, or live listing was created.</p>
            '''), 502

        # Save all 24 EPS URLs BEFORE the Inventory API call.
        # This lets us retry Inventory creation in the same Render process
        # without uploading the photos again if eBay returns a temporary error.
        EBAY_MEDIA_CACHE['LOT-001'] = eps_results

        image_urls = [result['image_url'] for result in eps_results]

        inventory_payload = {
            'availability': {
                'shipToLocationAvailability': {
                    'quantity': 1
                }
            },
            'condition': 'FOR_PARTS_OR_NOT_WORKING',
            'conditionDescription': (
                'Not working. Missing both original pinecone weights. '
                'Sold for repair, restoration, display, or parts. '
                'Please review all 24 photos carefully.'
            ),
            'product': {
                'title': (
                    'Vintage Soviet USSR Mayak Majak Cuckoo Clock '
                    'Serdobsk Mechanical FOR PARTS'
                ),
                'description': (
                    'Vintage Soviet mechanical cuckoo wall clock by Mayak/Majak, '
                    'Serdobsk Clock Factory, USSR, circa 1970s-1980s. '
                    'NOT WORKING. Missing two original pinecone weights. '
                    'Pendulum and two chains are present. An internal component '
                    'appears detached. Sold as-is for restoration, repair, display, '
                    'or parts. Please review all 24 photos for condition and completeness.'
                ),
                'imageUrls': image_urls,
            },
        }

        inventory_response = requests.put(
            f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Content-Language': 'en-US',
            },
            json=inventory_payload,
            timeout=60,
        )

        if inventory_response.status_code != 204:
            return page('LOT 001 Inventory Item failed', f'''
                <h2>24 photos reached EPS, but Inventory Item creation failed</h2>
                <p><b>HTTP {inventory_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(inventory_response.text[:8000])}</pre>
                <p><b>No Offer was created and nothing was published live.</b></p>
            '''), 502

        EBAY_MEDIA_CACHE['LOT-001'] = eps_results

        rows = []
        for result in eps_results:
            safe_url = html.escape(result['image_url'], quote=True)
            rows.append(
                '<tr>'
                f"<td>{result['number']}</td>"
                f"<td>{html.escape(result['filename'])}</td>"
                f'<td><a href="{safe_url}" target="_blank" rel="noopener">EPS image</a></td>'
                '</tr>'
            )

        return page('LOT 001 Inventory Item created', f'''
            <h2>LOT 001 — Inventory Item created successfully</h2>
            <p><b>SKU:</b> <code>{sku}</code></p>
            <p><b>EPS photos attached:</b> 24 / 24</p>
            <p><b>Quantity:</b> 1</p>
            <p><b>Condition:</b> FOR_PARTS_OR_NOT_WORKING</p>
            <p style="color:green;"><b>Inventory API returned HTTP 204 — success.</b></p>
            <p><b>No Offer was created. Nothing was published live on eBay.</b></p>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>#</th><th>File</th><th>EPS</th></tr>
                {''.join(rows)}
            </table>
        ''')

    except Exception as exc:
        return page('LOT 001 Inventory Item error', f'''
            <h2>LOT 001 — error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p>No Offer was created and nothing was published live.</p>
        '''), 500


@app.route('/ebay/retry-lot001-inventory', methods=['GET', 'POST'])
def ebay_retry_lot001_inventory():
    sku = 'LOT-001'
    eps_results = EBAY_MEDIA_CACHE.get(sku, [])
    image_urls = [
        item.get('image_url')
        for item in eps_results
        if item.get('image_url')
    ]

    if len(image_urls) != 24:
        return page('LOT 001 retry unavailable', '''
            <h2>LOT 001 — retry unavailable</h2>
            <p>The current Render process does not have all 24 EPS URLs cached.</p>
            <p>Please select the 24 Google Photos again and use
            <code>/ebay/create-lot001-inventory</code>.</p>
            <p>No Offer was created and nothing was published.</p>
        '''), 400

    try:
        access_token = get_ebay_access_token()
        inventory_url = (
            'https://api.ebay.com/sell/inventory/v1/inventory_item/LOT-001'
        )
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US',
        }

        # First check whether eBay actually created the item despite an earlier 500.
        check = requests.get(
            inventory_url,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json',
            },
            timeout=30,
        )

        if check.status_code == 200:
            data = check.json()
            product = data.get('product') or {}
            photos = product.get('imageUrls') or []
            title = product.get('title') or '(no title returned)'
            return page('LOT 001 already exists', f'''
                <h2>LOT 001 already exists in eBay Inventory</h2>
                <p><b>SKU:</b> LOT-001</p>
                <p><b>Title:</b> {html.escape(title)}</p>
                <p><b>Images returned by eBay:</b> {len(photos)}</p>
                <p><b>No replacement was sent.</b></p>
                <p><b>No Offer was created and nothing was published.</b></p>
            ''')

        if check.status_code not in (404,):
            return page('LOT 001 check failed', f'''
                <h2>Could not safely check LOT-001</h2>
                <p><b>HTTP {check.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(check.text[:8000])}</pre>
                <p>No Inventory replacement, Offer, or Publish request was sent.</p>
            '''), 502

        if request.method == 'GET':
            return page('Retry LOT 001 Inventory', '''
                <h2>LOT 001 — retry with cached EPS photos</h2>
                <p><b>Cached EPS photos: 24 / 24</b></p>
                <p>eBay currently reports that SKU <code>LOT-001</code> does not exist.</p>
                <p>The retry uses a minimal Inventory Item payload:</p>
                <ul>
                    <li>quantity 1</li>
                    <li>FOR_PARTS_OR_NOT_WORKING</li>
                    <li>condition description</li>
                    <li>title and description</li>
                    <li>24 EPS image URLs</li>
                </ul>
                <p><b>No package fields, locale, aspects, Offer, or Publish are included.</b></p>
                <form method="post">
                    <button type="submit" style="padding:12px 18px;font-size:16px">
                        Retry Inventory Item LOT-001
                    </button>
                </form>
            ''')

        payload = {
            'availability': {
                'shipToLocationAvailability': {
                    'quantity': 1
                }
            },
            'condition': 'FOR_PARTS_OR_NOT_WORKING',
            'conditionDescription': (
                'Not working. Missing both original pinecone weights. '
                'Sold for repair, restoration, display, or parts. '
                'Please review all 24 photos carefully.'
            ),
            'product': {
                'title': (
                    'Vintage Soviet USSR Mayak Majak Cuckoo Clock '
                    'Serdobsk Mechanical FOR PARTS'
                ),
                'description': (
                    'Vintage Soviet mechanical cuckoo wall clock by Mayak/Majak, '
                    'Serdobsk Clock Factory, USSR, circa 1970s-1980s. '
                    'NOT WORKING. Missing two original pinecone weights. '
                    'Pendulum and two chains are present. An internal component '
                    'appears detached. Sold as-is for restoration, repair, display, '
                    'or parts. Please review all 24 photos for condition and completeness.'
                ),
                'imageUrls': image_urls,
            },
        }

        response = requests.put(
            inventory_url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if response.status_code == 204:
            return page('LOT 001 Inventory created', '''
                <h2>LOT 001 — Inventory Item created successfully</h2>
                <p><b>HTTP 204</b></p>
                <p><b>SKU:</b> LOT-001</p>
                <p><b>Photos:</b> 24 EPS URLs</p>
                <p><b>No Offer was created and nothing was published live.</b></p>
            ''')

        return page('LOT 001 retry failed', f'''
            <h2>LOT 001 — retry failed</h2>
            <p><b>HTTP {response.status_code}</b></p>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(response.text[:8000])}</pre>
            <p>The 24 EPS URLs remain cached while this Render process stays alive.</p>
            <p><b>No Offer was created and nothing was published.</b></p>
        '''), 502

    except Exception as exc:
        return page('LOT 001 retry error', f'''
            <h2>LOT 001 — retry error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p>No Offer was created and nothing was published.</p>
        '''), 500


# =========================================================
# EBAY TAXONOMY — LOT 001
# Read-only discovery: category suggestions + aspects.
# NO Inventory replacement, NO Offer, NO Publish.
# =========================================================

@app.route('/ebay/lot001-taxonomy')
def ebay_lot001_taxonomy():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Accept-Language': 'en-US',
        }

        # 1) Always ask eBay for the current default EBAY_US category tree.
        tree_response = requests.get(
            'https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id',
            headers=headers,
            params={'marketplace_id': 'EBAY_US'},
            timeout=30,
        )
        if not tree_response.ok:
            return page('LOT 001 Taxonomy error', f'''
                <h2>Could not get EBAY_US category tree</h2>
                <p><b>HTTP {tree_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(tree_response.text[:8000])}</pre>
                <p>No Inventory Item, Offer, or listing was changed.</p>
            '''), 502

        tree_data = tree_response.json()
        tree_id = tree_data.get('categoryTreeId')
        tree_version = tree_data.get('categoryTreeVersion', '')

        if not tree_id:
            return page('LOT 001 Taxonomy error', '''
                <h2>eBay did not return a categoryTreeId.</h2>
                <p>No Inventory Item, Offer, or listing was changed.</p>
            '''), 502

        # Use several factual keyword formulations for this specific item.
        # We display eBay's suggestions rather than silently hard-coding a category.
        queries = [
            'vintage cuckoo clock',
            'mechanical cuckoo wall clock',
            'Soviet USSR cuckoo clock Mayak',
        ]

        suggestion_sets = []
        candidate_ids = []
        candidate_names = {}

        for q in queries:
            response = requests.get(
                f'https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions',
                headers=headers,
                params={'q': q},
                timeout=30,
            )

            if not response.ok:
                suggestion_sets.append({
                    'query': q,
                    'error_status': response.status_code,
                    'error_text': response.text[:4000],
                    'suggestions': [],
                })
                continue

            data = response.json()
            suggestions = data.get('categorySuggestions') or []
            suggestion_sets.append({
                'query': q,
                'suggestions': suggestions,
            })

            for suggestion in suggestions[:5]:
                category = suggestion.get('category') or {}
                cid = str(category.get('categoryId') or '')
                cname = category.get('categoryName') or ''
                if cid:
                    candidate_ids.append(cid)
                    candidate_names[cid] = cname

        if not candidate_ids:
            error_blocks = []
            for result in suggestion_sets:
                if result.get('error_status'):
                    error_blocks.append(
                        f"<p><b>{html.escape(result['query'])}</b>: "
                        f"HTTP {result['error_status']}</p>"
                        f"<pre>{html.escape(result.get('error_text',''))}</pre>"
                    )
            return page('LOT 001 Taxonomy', f'''
                <h2>No category suggestions were returned</h2>
                {''.join(error_blocks)}
                <p>No Inventory Item, Offer, or listing was changed.</p>
            '''), 502

        # Score candidates by recurrence and rank across the three eBay searches.
        scores = {}
        appearances = {}
        for result in suggestion_sets:
            for rank, suggestion in enumerate(result.get('suggestions', [])[:5], start=1):
                category = suggestion.get('category') or {}
                cid = str(category.get('categoryId') or '')
                if not cid:
                    continue
                scores[cid] = scores.get(cid, 0) + (6 - rank)
                appearances[cid] = appearances.get(cid, 0) + 1

        ranked_ids = sorted(
            set(candidate_ids),
            key=lambda cid: (appearances.get(cid, 0), scores.get(cid, 0)),
            reverse=True,
        )
        selected_id = ranked_ids[0]
        selected_name = candidate_names.get(selected_id, '')

        # 2) Ask eBay for the exact aspects belonging to the top leaf category.
        aspects_response = requests.get(
            f'https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category',
            headers=headers,
            params={'category_id': selected_id},
            timeout=30,
        )

        aspects = []
        aspects_error = ''
        if aspects_response.ok:
            aspects = aspects_response.json().get('aspects') or []
        else:
            aspects_error = (
                f'HTTP {aspects_response.status_code}: '
                f'{aspects_response.text[:5000]}'
            )

        # Render category suggestions with full breadcrumb paths.
        suggestion_html = []
        for result in suggestion_sets:
            q = result['query']
            if result.get('error_status'):
                suggestion_html.append(
                    f'<h3>{html.escape(q)}</h3>'
                    f'<p>HTTP {result["error_status"]}</p>'
                    f'<pre style="white-space:pre-wrap;word-break:break-word;">'
                    f'{html.escape(result.get("error_text",""))}</pre>'
                )
                continue

            rows = []
            for rank, suggestion in enumerate(result.get('suggestions', [])[:5], start=1):
                category = suggestion.get('category') or {}
                cid = str(category.get('categoryId') or '')
                cname = category.get('categoryName') or ''
                ancestors = suggestion.get('categoryTreeNodeAncestors') or []
                path_names = [
                    (a.get('category') or {}).get('categoryName', '')
                    for a in ancestors
                ]
                path_names = [p for p in path_names if p]
                path = ' > '.join(path_names + [cname])
                selected_mark = ' <b>← selected candidate</b>' if cid == selected_id else ''
                rows.append(
                    '<tr>'
                    f'<td>{rank}</td>'
                    f'<td>{html.escape(cid)}</td>'
                    f'<td>{html.escape(cname)}</td>'
                    f'<td>{html.escape(path)}</td>'
                    f'<td>{appearances.get(cid,0)}</td>'
                    f'<td>{scores.get(cid,0)}</td>'
                    f'<td>{selected_mark}</td>'
                    '</tr>'
                )

            suggestion_html.append(
                f'<h3>Query: {html.escape(q)}</h3>'
                '<table border="1" cellpadding="5" cellspacing="0">'
                '<tr><th>#</th><th>ID</th><th>Category</th><th>Path</th>'
                '<th>Appearances</th><th>Score</th><th></th></tr>'
                + ''.join(rows) +
                '</table>'
            )

        # Render required aspects first, then recommended/optional.
        aspect_rows = []
        for aspect in aspects:
            name = aspect.get('localizedAspectName') or ''
            constraint = aspect.get('aspectConstraint') or {}
            required = bool(constraint.get('aspectRequired'))
            usage = constraint.get('aspectUsage') or ''
            cardinality = constraint.get('itemToAspectCardinality') or ''
            mode = constraint.get('aspectMode') or ''
            values = [
                v.get('localizedValue')
                for v in (aspect.get('aspectValues') or [])
                if v.get('localizedValue')
            ]
            sample_values = ', '.join(values[:12])
            if len(values) > 12:
                sample_values += f' … (+{len(values)-12})'

            aspect_rows.append({
                'name': name,
                'required': required,
                'usage': usage,
                'cardinality': cardinality,
                'mode': mode,
                'values': sample_values,
            })

        aspect_rows.sort(
            key=lambda a: (
                not a['required'],
                a['usage'] != 'RECOMMENDED',
                a['name'].lower(),
            )
        )

        rendered_aspects = []
        for a in aspect_rows:
            rendered_aspects.append(
                '<tr>'
                f"<td>{html.escape(a['name'])}</td>"
                f"<td>{'YES' if a['required'] else 'No'}</td>"
                f"<td>{html.escape(a['usage'])}</td>"
                f"<td>{html.escape(a['cardinality'])}</td>"
                f"<td>{html.escape(a['mode'])}</td>"
                f"<td>{html.escape(a['values'])}</td>"
                '</tr>'
            )

        aspects_section = (
            '<p><b>Aspect request error:</b> '
            + html.escape(aspects_error)
            + '</p>'
            if aspects_error
            else (
                '<table border="1" cellpadding="5" cellspacing="0">'
                '<tr><th>Aspect</th><th>Required</th><th>Usage</th>'
                '<th>Cardinality</th><th>Mode</th><th>eBay values (sample)</th></tr>'
                + ''.join(rendered_aspects) +
                '</table>'
            )
        )

        return page('LOT 001 Taxonomy', f'''
            <h2>LOT 001 — eBay Taxonomy result</h2>
            <p><b>Marketplace:</b> EBAY_US</p>
            <p><b>Current category tree:</b> {html.escape(str(tree_id))}
               &nbsp; <b>version:</b> {html.escape(str(tree_version))}</p>
            <p><b>Top candidate from the combined eBay suggestions:</b>
               {html.escape(selected_name)} (ID {html.escape(selected_id)})</p>
            <p>
                This page is diagnostic/read-only. The top candidate is displayed
                for review; it is <b>not</b> written into an Offer automatically.
            </p>

            <h2>Category suggestions returned by eBay</h2>
            {''.join(suggestion_html)}

            <h2>Item Specifics for candidate {html.escape(selected_id)}</h2>
            {aspects_section}

            <hr>
            <p><b>No Inventory Item was replaced. No Offer was created.
            Nothing was published live.</b></p>
        ''')

    except Exception as exc:
        return page('LOT 001 Taxonomy error', f'''
            <h2>LOT 001 — Taxonomy error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p>No Inventory Item, Offer, or listing was changed.</p>
        '''), 500


# =========================================================
# LOT 001 — SAFE INVENTORY UPDATE WITH VERIFIED ITEM SPECIFICS
# Category 261604 is NOT written to Inventory Item; it belongs to Offer later.
# This route only GETs current LOT-001, preserves existing fields/images,
# and on explicit POST adds/updates product aspects.
# NO Offer, NO Publish.
# =========================================================

@app.route('/ebay/update-lot001-specifics', methods=['GET', 'POST'])
def ebay_update_lot001_specifics():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US',
        }

        item_url = 'https://api.ebay.com/sell/inventory/v1/inventory_item/LOT-001'

        # Always fetch the authoritative current Inventory Item first.
        current_response = requests.get(item_url, headers=headers, timeout=30)
        if current_response.status_code != 200:
            return page('LOT 001 update error', f"""
                <h2>Could not read current Inventory Item LOT-001</h2>
                <p><b>HTTP {current_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(current_response.text[:8000])}</pre>
                <p><b>No update was sent. No Offer was created. Nothing was published.</b></p>
            """), 502

        current = current_response.json()
        current_product = current.get('product') or {}
        current_images = current_product.get('imageUrls') or []

        if len(current_images) != 24:
            return page('LOT 001 safety stop', f"""
                <h2>Safety stop</h2>
                <p>eBay currently returned <b>{len(current_images)} / 24</b> image URLs for LOT-001.</p>
                <p>The Inventory Item will not be replaced until all 24 existing images are confirmed.</p>
                <p><b>No update was sent. No Offer was created. Nothing was published.</b></p>
            """), 409

        # Conservative specifics supported by the photos/research already completed.
        # Do not claim an exact formal model number that has not been verified.
        lot001_aspects = {
            'Brand': ['Mayak'],
            'Antique': ['No'],
            'Country of Origin': ['Ukraine'],
            'Era': ['Late 20th Century (1970-1999)'],
            'Material': ['Plastic', 'Wood', 'Metal'],
            'Movement': ['Mechanical'],
            'Power Source': ['Mechanical'],
            'Time Period Manufactured': ['1970-1979'],
            'Type': ['Cuckoo Clock'],
            'Vintage': ['Yes'],
        }

        if request.method == 'GET':
            rows = ''.join(
                '<tr><td>' + html.escape(k) + '</td><td>' +
                html.escape(', '.join(v)) + '</td></tr>'
                for k, v in lot001_aspects.items()
            )
            return page('LOT 001 — confirm Item Specifics update', f"""
                <h2>LOT 001 — confirm Inventory Item update</h2>
                <p><b>SKU:</b> LOT-001</p>
                <p><b>Current EPS images confirmed by eBay:</b> {len(current_images)} / 24</p>
                <p><b>Verified future Offer category:</b> 261604 — Cuckoo &amp; Black Forest Clocks</p>
                <p><b>Important:</b> category 261604 is <u>not</u> sent in this Inventory Item update.
                eBay assigns the category when the Offer is created later.</p>

                <h3>Item Specifics to add/update</h3>
                <table border="1" cellpadding="5" cellspacing="0">
                    <tr><th>Aspect</th><th>Value</th></tr>
                    {rows}
                </table>

                <p>The update preserves the current Inventory Item fields and all 24 eBay image URLs.</p>
                <form method="post">
                    <button type="submit">Confirm: update LOT-001 Item Specifics</button>
                </form>
                <p><b>No Offer will be created. Nothing will be published live.</b></p>
            """)

        # PUT createOrReplaceInventoryItem is replacement semantics.
        # Build from the authoritative GET response so existing fields/images survive.
        payload = {}

        for key in (
            'availability',
            'condition',
            'conditionDescription',
            'packageWeightAndSize',
            'product',
        ):
            if key in current:
                payload[key] = current[key]

        product = dict(payload.get('product') or {})
        product['imageUrls'] = current_images

        existing_aspects = dict(product.get('aspects') or {})
        existing_aspects.update(lot001_aspects)
        product['aspects'] = existing_aspects
        payload['product'] = product

        update_response = requests.put(
            item_url,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if update_response.status_code != 204:
            return page('LOT 001 update failed', f"""
                <h2>Inventory Item update failed</h2>
                <p><b>HTTP {update_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(update_response.text[:10000])}</pre>
                <p><b>No Offer was created. Nothing was published live.</b></p>
            """), 502

        # Verify the saved state immediately from eBay.
        verify_response = requests.get(item_url, headers=headers, timeout=30)
        if verify_response.status_code != 200:
            return page('LOT 001 updated; verification error', f"""
                <h2>Inventory update returned HTTP 204, but verification GET failed.</h2>
                <p><b>Verification HTTP {verify_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(verify_response.text[:8000])}</pre>
                <p>No Offer was created. Nothing was published live.</p>
            """), 502

        verified = verify_response.json()
        verified_product = verified.get('product') or {}
        verified_images = verified_product.get('imageUrls') or []
        verified_aspects = verified_product.get('aspects') or {}

        aspect_rows = ''.join(
            '<tr><td>' + html.escape(str(k)) + '</td><td>' +
            html.escape(', '.join(v) if isinstance(v, list) else str(v)) +
            '</td></tr>'
            for k, v in verified_aspects.items()
        )

        return page('LOT 001 — Item Specifics updated', f"""
            <h2>LOT 001 — Inventory Item updated successfully</h2>
            <p><b>Inventory API returned HTTP 204 — success.</b></p>
            <p><b>Verification GET:</b> HTTP 200</p>
            <p><b>EPS images still attached:</b> {len(verified_images)} / 24</p>
            <p><b>Condition:</b> {html.escape(str(verified.get('condition', '')))}</p>
            <p><b>Quantity:</b> {html.escape(str(((verified.get('availability') or {}).get('shipToLocationAvailability') or {}).get('quantity', '')))}</p>

            <h3>Saved Item Specifics</h3>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Aspect</th><th>Saved value</th></tr>
                {aspect_rows}
            </table>

            <p><b>Future Offer category:</b> 261604 — Cuckoo &amp; Black Forest Clocks</p>
            <p><b>No Offer was created. Nothing was published live.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 update error', f"""
            <h2>LOT 001 — update error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p>No Offer was created. Nothing was published live.</p>
        """), 500


# =========================================================
# LOT 001 — CREATE EBAY OFFER SAFELY (NO PUBLISH)
# Uses verified category/policies/location/price.
# GET checks whether an Offer already exists for SKU LOT-001.
# POST creates exactly one unpublished Offer, then verifies it.
# =========================================================

@app.route('/ebay/create-lot001-offer', methods=['GET', 'POST'])
def ebay_create_lot001_offer():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Content-Language': 'en-US',
        }

        inventory_url = 'https://api.ebay.com/sell/inventory/v1/inventory_item/LOT-001'
        offers_url = 'https://api.ebay.com/sell/inventory/v1/offer'

        # Safety check 1: authoritative Inventory Item must exist and still have 24 images.
        inv_response = requests.get(inventory_url, headers=headers, timeout=30)
        if inv_response.status_code != 200:
            return page('LOT 001 Offer safety stop', f"""
                <h2>Safety stop — Inventory Item could not be read</h2>
                <p><b>HTTP {inv_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(inv_response.text[:8000])}</pre>
                <p><b>No Offer was created. Nothing was published.</b></p>
            """), 502

        inv = inv_response.json()
        images = ((inv.get('product') or {}).get('imageUrls') or [])
        aspects = ((inv.get('product') or {}).get('aspects') or {})
        if len(images) != 24 or not aspects.get('Brand'):
            return page('LOT 001 Offer safety stop', f"""
                <h2>Safety stop</h2>
                <p>Current eBay Inventory Item returned <b>{len(images)} / 24</b> images.</p>
                <p>Brand Item Specific present: <b>{'YES' if aspects.get('Brand') else 'NO'}</b></p>
                <p><b>No Offer was created. Nothing was published.</b></p>
            """), 409

        # Safety check 2: do not create a duplicate Offer for this SKU/marketplace.
        existing_response = requests.get(
            offers_url,
            headers=headers,
            params={'sku': 'LOT-001', 'marketplace_id': 'EBAY_US'},
            timeout=30,
        )
        # eBay may return 404 / errorId 25713 when there is no Offer for the SKU.
        # For this duplicate check, that is a valid "no existing Offer" result.
        if existing_response.status_code == 404:
            try:
                existing_error_data = existing_response.json()
            except Exception:
                existing_error_data = {}
            existing_errors = existing_error_data.get('errors') or []
            error_ids = {str(e.get('errorId')) for e in existing_errors}
            if '25713' in error_ids:
                existing_data = {'offers': []}
            else:
                return page('LOT 001 Offer safety stop', f"""
                    <h2>Could not check existing Offers</h2>
                    <p><b>HTTP {existing_response.status_code}</b></p>
                    <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(existing_response.text[:8000])}</pre>
                    <p><b>No Offer was created. Nothing was published.</b></p>
                """), 502
        elif existing_response.status_code == 200:
            existing_data = existing_response.json()
        else:
            return page('LOT 001 Offer safety stop', f"""
                <h2>Could not check existing Offers</h2>
                <p><b>HTTP {existing_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(existing_response.text[:8000])}</pre>
                <p><b>No Offer was created. Nothing was published.</b></p>
            """), 502

        existing_offers = existing_data.get('offers') or []
        if existing_offers:
            rows = []
            for offer in existing_offers:
                rows.append(
                    '<tr>'
                    f"<td>{html.escape(str(offer.get('offerId','')))}</td>"
                    f"<td>{html.escape(str(offer.get('status','')))}</td>"
                    f"<td>{html.escape(str(offer.get('categoryId','')))}</td>"
                    f"<td>{html.escape(str((offer.get('pricingSummary') or {}).get('price',{}).get('value','')))}</td>"
                    '</tr>'
                )
            return page('LOT 001 — Offer already exists', f"""
                <h2>LOT 001 — existing Offer detected</h2>
                <p>For safety, no duplicate Offer was created.</p>
                <table border="1" cellpadding="5" cellspacing="0">
                  <tr><th>Offer ID</th><th>Status</th><th>Category</th><th>Price USD</th></tr>
                  {''.join(rows)}
                </table>
                <p><b>Nothing was published by this route.</b></p>
            """)

        offer_payload = {
            'sku': 'LOT-001',
            'marketplaceId': 'EBAY_US',
            'format': 'FIXED_PRICE',
            'availableQuantity': 1,
            'categoryId': '261604',
            'merchantLocationKey': 'vintage-ua-warehouse-1',
            'listingDescription': (
                'Vintage Soviet mechanical cuckoo wall clock by Mayak/Majak, '
                'Serdobsk Clock Factory, USSR, circa 1970s-1980s. '
                'NOT WORKING. Missing two original pinecone weights. '
                'Pendulum and two chains are present. An internal component appears detached. '
                'Sold as-is for restoration, repair, display, or parts. '
                'Please review all 24 photos for condition and completeness.'
            ),
            'listingPolicies': {
                'paymentPolicyId': '275629409012',
                'returnPolicyId': '275629453012',
                'fulfillmentPolicyId': '275629516012',
            },
            'pricingSummary': {
                'price': {
                    'value': '49.99',
                    'currency': 'USD',
                }
            },
        }

        if request.method == 'GET':
            return page('LOT 001 — confirm Offer creation', """
                <h2>LOT 001 — confirm eBay Offer creation</h2>
                <p><b>Inventory Item:</b> LOT-001 — verified</p>
                <p><b>EPS images:</b> 24 / 24 — verified directly from eBay</p>
                <p><b>Marketplace:</b> EBAY_US</p>
                <p><b>Format:</b> FIXED_PRICE</p>
                <p><b>Quantity:</b> 1</p>
                <p><b>Category:</b> 261604 — Cuckoo &amp; Black Forest Clocks</p>
                <p><b>Price:</b> $49.99 USD</p>
                <p><b>Inventory location:</b> vintage-ua-warehouse-1</p>
                <p><b>Payment policy:</b> 275629409012</p>
                <p><b>Return policy:</b> 275629453012</p>
                <p><b>Fulfillment policy:</b> 275629516012 — flat shipping $49.99 to USA</p>
                <p><b>Existing Offer for LOT-001:</b> none</p>
                <form method="post">
                    <button type="submit">Confirm: create unpublished Offer for LOT-001</button>
                </form>
                <p><b>This route does NOT call publishOffer. Nothing will be listed live.</b></p>
            """)

        create_response = requests.post(
            offers_url,
            headers=headers,
            json=offer_payload,
            timeout=60,
        )

        if create_response.status_code not in (200, 201):
            return page('LOT 001 Offer creation failed', f"""
                <h2>Offer creation failed</h2>
                <p><b>HTTP {create_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(create_response.text[:12000])}</pre>
                <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
            """), 502

        try:
            created = create_response.json()
        except Exception:
            created = {}
        offer_id = str(created.get('offerId') or '')

        # Verify using GET offers by SKU rather than publishing.
        verify_response = requests.get(
            offers_url,
            headers=headers,
            params={'sku': 'LOT-001', 'marketplace_id': 'EBAY_US'},
            timeout=30,
        )
        if verify_response.status_code != 200:
            return page('LOT 001 Offer created; verification error', f"""
                <h2>Offer creation succeeded, but verification GET failed</h2>
                <p><b>Created Offer ID:</b> {html.escape(offer_id)}</p>
                <p><b>Verification HTTP {verify_response.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(verify_response.text[:8000])}</pre>
                <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
            """), 502

        verify_data = verify_response.json()
        verified_offers = verify_data.get('offers') or []
        verified = None
        if offer_id:
            verified = next(
                (o for o in verified_offers if str(o.get('offerId')) == offer_id),
                None
            )
        if verified is None and verified_offers:
            verified = verified_offers[0]
        verified = verified or {}

        price = ((verified.get('pricingSummary') or {}).get('price') or {})
        policies = verified.get('listingPolicies') or {}

        return page('LOT 001 — Offer created successfully', f"""
            <h2>LOT 001 — unpublished Offer created successfully</h2>
            <p><b>Create Offer HTTP:</b> {create_response.status_code}</p>
            <p><b>Offer ID:</b> {html.escape(str(verified.get('offerId') or offer_id))}</p>
            <p><b>Status:</b> {html.escape(str(verified.get('status','')))}</p>
            <p><b>SKU:</b> {html.escape(str(verified.get('sku','')))}</p>
            <p><b>Marketplace:</b> {html.escape(str(verified.get('marketplaceId','')))}</p>
            <p><b>Category:</b> {html.escape(str(verified.get('categoryId','')))}</p>
            <p><b>Price:</b> {html.escape(str(price.get('value','')))} {html.escape(str(price.get('currency','')))}</p>
            <p><b>Quantity:</b> {html.escape(str(verified.get('availableQuantity','')))}</p>
            <p><b>Payment policy:</b> {html.escape(str(policies.get('paymentPolicyId','')))}</p>
            <p><b>Return policy:</b> {html.escape(str(policies.get('returnPolicyId','')))}</p>
            <p><b>Fulfillment policy:</b> {html.escape(str(policies.get('fulfillmentPolicyId','')))}</p>
            <p><b>Verification GET:</b> HTTP 200</p>
            <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 Offer error', f"""
            <h2>LOT 001 — Offer error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p><b>publishOffer was NOT called by this route.</b></p>
        """), 500


# =========================================================
# LOT 001 — READ-ONLY PRE-PUBLISH CHECK
# Reads authoritative Inventory Item + Offer directly from eBay.
# NO PUT/POST/DELETE. NO publishOffer.
# =========================================================

@app.route('/ebay/lot001-preflight')
def ebay_lot001_preflight():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Language': 'en-US',
        }

        sku = 'LOT-001'
        offer_id = '264904072011'

        inv_url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
        offer_url = f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'

        inv_r = requests.get(inv_url, headers=headers, timeout=30)
        offer_r = requests.get(offer_url, headers=headers, timeout=30)

        if inv_r.status_code != 200 or offer_r.status_code != 200:
            return page('LOT 001 preflight error', f"""
                <h2>LOT 001 — pre-publish read failed</h2>
                <p><b>Inventory GET:</b> HTTP {inv_r.status_code}</p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(inv_r.text[:6000])}</pre>
                <p><b>Offer GET:</b> HTTP {offer_r.status_code}</p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(offer_r.text[:6000])}</pre>
                <p><b>No data was changed. publishOffer was NOT called.</b></p>
            """), 502

        inv = inv_r.json()
        offer = offer_r.json()

        product = inv.get('product') or {}
        images = product.get('imageUrls') or []
        aspects = product.get('aspects') or {}
        policies = offer.get('listingPolicies') or {}
        price = ((offer.get('pricingSummary') or {}).get('price') or {})

        title = str(product.get('title') or '')
        description = str(offer.get('listingDescription') or product.get('description') or '')
        condition = str(inv.get('condition') or '')
        condition_description = str(inv.get('conditionDescription') or '')

        expected = {
            'offerId': offer_id,
            'sku': sku,
            'marketplaceId': 'EBAY_US',
            'status': 'UNPUBLISHED',
            'format': 'FIXED_PRICE',
            'categoryId': '261604',
            'merchantLocationKey': 'vintage-ua-warehouse-1',
            'availableQuantity': 1,
            'price': '49.99',
            'currency': 'USD',
            'paymentPolicyId': '275629409012',
            'returnPolicyId': '275629453012',
            'fulfillmentPolicyId': '275629516012',
            'condition': 'FOR_PARTS_OR_NOT_WORKING',
        }

        checks = []

        def add_check(name, actual, wanted, ok=None):
            if ok is None:
                ok = str(actual) == str(wanted)
            checks.append((name, actual, wanted, bool(ok)))

        add_check('Offer ID', offer.get('offerId'), expected['offerId'])
        add_check('SKU', offer.get('sku'), expected['sku'])
        add_check('Marketplace', offer.get('marketplaceId'), expected['marketplaceId'])
        add_check('Offer status', offer.get('status'), expected['status'])
        add_check('Format', offer.get('format'), expected['format'])
        add_check('Category ID', offer.get('categoryId'), expected['categoryId'])
        add_check('Inventory location', offer.get('merchantLocationKey'), expected['merchantLocationKey'])
        add_check('Available quantity', offer.get('availableQuantity'), expected['availableQuantity'])
        add_check('Price', price.get('value'), expected['price'],
                  str(price.get('value')) in ('49.99', '49.990', '49.9900'))
        add_check('Currency', price.get('currency'), expected['currency'])
        add_check('Payment policy', policies.get('paymentPolicyId'), expected['paymentPolicyId'])
        add_check('Return policy', policies.get('returnPolicyId'), expected['returnPolicyId'])
        add_check('Fulfillment policy', policies.get('fulfillmentPolicyId'), expected['fulfillmentPolicyId'])
        add_check('Condition', condition, expected['condition'])
        add_check('EPS image count', len(images), 24, len(images) == 24)
        add_check('Required Brand aspect', ', '.join(aspects.get('Brand') or []), 'Mayak',
                  bool(aspects.get('Brand')))

        # Content checks: ensure the buyer-facing text does not hide the known defects.
        title_ok = bool(title) and len(title) <= 80
        add_check('Title present / <= 80 chars', f'{len(title)} chars: {title}', '<=80 chars', title_ok)

        desc_lower = description.lower()
        defect_terms_ok = (
            ('not working' in desc_lower)
            and ('missing' in desc_lower)
            and ('pinecone' in desc_lower or 'weights' in desc_lower)
        )
        add_check('Description discloses non-working + missing weights',
                  'YES' if defect_terms_ok else 'NO', 'YES', defect_terms_ok)

        cond_lower = condition_description.lower()
        condition_text_ok = (
            ('not working' in cond_lower)
            and ('missing' in cond_lower)
            and ('weight' in cond_lower)
        )
        add_check('Condition description discloses defects',
                  'YES' if condition_text_ok else 'NO', 'YES', condition_text_ok)

        all_ok = all(c[3] for c in checks)

        rows = ''.join(
            '<tr>'
            f'<td>{"PASS" if ok else "FAIL"}</td>'
            f'<td>{html.escape(str(name))}</td>'
            f'<td>{html.escape(str(actual))}</td>'
            f'<td>{html.escape(str(wanted))}</td>'
            '</tr>'
            for name, actual, wanted, ok in checks
        )

        aspect_rows = ''.join(
            '<tr><td>' + html.escape(str(k)) + '</td><td>' +
            html.escape(', '.join(v) if isinstance(v, list) else str(v)) +
            '</td></tr>'
            for k, v in aspects.items()
        )

        image_rows = ''.join(
            f'<li>#{i}: <a href="{html.escape(str(url), quote=True)}" target="_blank" rel="noopener">EPS image</a></li>'
            for i, url in enumerate(images, start=1)
        )

        status_text = (
            'PRE-FLIGHT PASSED — technical data is consistent.'
            if all_ok
            else 'PRE-FLIGHT STOP — one or more checks failed.'
        )

        return page('LOT 001 — pre-publish check', f"""
            <h2>LOT 001 — final pre-publish check</h2>
            <h3>{html.escape(status_text)}</h3>
            <p><b>This page is READ-ONLY.</b> It only performed GET requests to eBay.</p>

            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Result</th><th>Check</th><th>Actual from eBay</th><th>Expected</th></tr>
                {rows}
            </table>

            <h3>Buyer-facing title</h3>
            <p>{html.escape(title)}</p>

            <h3>Buyer-facing listing description</h3>
            <div style="white-space:pre-wrap;border:1px solid #ccc;padding:8px;">{html.escape(description)}</div>

            <h3>Condition description</h3>
            <div style="white-space:pre-wrap;border:1px solid #ccc;padding:8px;">{html.escape(condition_description)}</div>

            <h3>Saved Item Specifics</h3>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Aspect</th><th>Value</th></tr>
                {aspect_rows}
            </table>

            <h3>EPS photos ({len(images)} / 24)</h3>
            <ol>{image_rows}</ol>

            <hr>
            <p><b>Inventory GET:</b> HTTP 200</p>
            <p><b>Offer GET:</b> HTTP 200</p>
            <p><b>Offer status:</b> {html.escape(str(offer.get('status','')))}</p>
            <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 preflight error', f"""
            <h2>LOT 001 — preflight error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p><b>No data was changed. publishOffer was NOT called.</b></p>
        """), 500


# =========================================================
# LOT 001 — SAFE SEARCH-OPTIMIZATION OF ITEM SPECIFICS
# Removes only two unsupported/over-specific assertions.
# GET = preview, POST = confirmed Inventory Item replacement.
# Does NOT modify Offer and NEVER calls publishOffer.
# =========================================================

@app.route('/ebay/optimize-lot001-specifics', methods=['GET', 'POST'])
def ebay_optimize_lot001_specifics():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Language': 'en-US',
        }
        sku = 'LOT-001'
        inv_url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'

        current_r = requests.get(inv_url, headers=headers, timeout=30)
        if current_r.status_code != 200:
            return page('LOT 001 safety stop', f"""
                <h2>Could not read LOT-001 Inventory Item</h2>
                <p><b>HTTP {current_r.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(current_r.text[:8000])}</pre>
                <p><b>Nothing was changed. Nothing was published.</b></p>
            """), 502

        current = current_r.json()
        product = current.get('product') or {}
        images = product.get('imageUrls') or []
        aspects = dict(product.get('aspects') or {})

        # Strong safety guards: do not replace the Inventory Item if the
        # authoritative object no longer looks like the verified LOT-001.
        if len(images) != 24:
            return page('LOT 001 safety stop', f"""
                <h2>Safety stop</h2>
                <p>Expected exactly <b>24 EPS images</b>; eBay currently returned <b>{len(images)}</b>.</p>
                <p><b>Nothing was changed. Nothing was published.</b></p>
            """), 409

        if not aspects.get('Brand'):
            return page('LOT 001 safety stop', """
                <h2>Safety stop</h2>
                <p>Brand aspect is missing from the authoritative Inventory Item.</p>
                <p><b>Nothing was changed. Nothing was published.</b></p>
            """), 409

        old_country = aspects.get('Country of Origin')
        old_period = aspects.get('Time Period Manufactured')

        # Keep high-confidence search specifics, remove only disputed assertions.
        aspects.pop('Country of Origin', None)
        aspects.pop('Time Period Manufactured', None)

        new_product = dict(product)
        new_product['aspects'] = aspects

        # createOrReplaceInventoryItem is replace semantics, so preserve every
        # existing top-level Inventory Item field that belongs in the PUT body.
        allowed_fields = {
            'availability', 'condition', 'conditionDescription', 'locale',
            'packageWeightAndSize', 'product'
        }
        payload = {k: v for k, v in current.items() if k in allowed_fields}
        payload['product'] = new_product

        aspect_rows = ''.join(
            '<tr><td>' + html.escape(str(k)) + '</td><td>' +
            html.escape(', '.join(v) if isinstance(v, list) else str(v)) +
            '</td></tr>'
            for k, v in aspects.items()
        )

        if request.method == 'GET':
            return page('LOT 001 — optimize specifics', f"""
                <h2>LOT 001 — safe search optimization</h2>
                <p><b>SKU:</b> LOT-001</p>
                <p><b>EPS photos preserved:</b> {len(images)} / 24</p>

                <h3>Only these values will be removed</h3>
                <table border="1" cellpadding="5" cellspacing="0">
                    <tr><th>Aspect</th><th>Current value</th><th>Action</th></tr>
                    <tr><td>Country of Origin</td><td>{html.escape(str(old_country))}</td><td>REMOVE</td></tr>
                    <tr><td>Time Period Manufactured</td><td>{html.escape(str(old_period))}</td><td>REMOVE</td></tr>
                </table>

                <h3>These Item Specifics will remain</h3>
                <table border="1" cellpadding="5" cellspacing="0">
                    <tr><th>Aspect</th><th>Value</th></tr>
                    {aspect_rows}
                </table>

                <p><b>Title, description, condition, quantity and all 24 EPS photos are preserved.</b></p>
                <p><b>The existing Offer is NOT modified. Price $49.99 and shipping policy remain unchanged.</b></p>
                <p><b>publishOffer is NOT called.</b></p>

                <form method="post">
                    <button type="submit" style="padding:12px 18px;font-size:16px;">
                        Confirm: remove only these 2 Item Specifics
                    </button>
                </form>
            """)

        put_headers = dict(headers)
        put_headers['Content-Type'] = 'application/json'
        put_r = requests.put(inv_url, headers=put_headers, json=payload, timeout=45)

        if put_r.status_code != 204:
            return page('LOT 001 update failed', f"""
                <h2>Inventory update failed</h2>
                <p><b>HTTP {put_r.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(put_r.text[:8000])}</pre>
                <p><b>publishOffer was NOT called.</b></p>
            """), 502

        verify_r = requests.get(inv_url, headers=headers, timeout=30)
        if verify_r.status_code != 200:
            return page('LOT 001 verification failed', f"""
                <h2>Update returned HTTP 204, but verification GET failed</h2>
                <p><b>GET HTTP {verify_r.status_code}</b></p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(verify_r.text[:8000])}</pre>
                <p><b>publishOffer was NOT called.</b></p>
            """), 502

        verified = verify_r.json()
        verified_product = verified.get('product') or {}
        verified_aspects = verified_product.get('aspects') or {}
        verified_images = verified_product.get('imageUrls') or []

        removed_ok = (
            'Country of Origin' not in verified_aspects
            and 'Time Period Manufactured' not in verified_aspects
        )
        images_ok = len(verified_images) == 24

        if not (removed_ok and images_ok):
            return page('LOT 001 verification warning', f"""
                <h2>Verification warning</h2>
                <p><b>Removed fields verified:</b> {removed_ok}</p>
                <p><b>EPS photos:</b> {len(verified_images)} / 24</p>
                <p><b>Do NOT publish yet.</b></p>
                <p><b>publishOffer was NOT called.</b></p>
            """), 409

        final_rows = ''.join(
            '<tr><td>' + html.escape(str(k)) + '</td><td>' +
            html.escape(', '.join(v) if isinstance(v, list) else str(v)) +
            '</td></tr>'
            for k, v in verified_aspects.items()
        )

        return page('LOT 001 — optimization complete', f"""
            <h2>LOT 001 — Inventory Item optimized successfully</h2>
            <p><b>Inventory API PUT:</b> HTTP 204</p>
            <p><b>Verification GET:</b> HTTP 200</p>
            <p><b>EPS photos preserved:</b> {len(verified_images)} / 24</p>
            <p><b>Country of Origin:</b> removed</p>
            <p><b>Time Period Manufactured:</b> removed</p>

            <h3>Current Item Specifics</h3>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Aspect</th><th>Value</th></tr>
                {final_rows}
            </table>

            <p><b>Existing Offer was not modified.</b></p>
            <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 optimization error', f"""
            <h2>LOT 001 — optimization error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p><b>publishOffer was NOT called.</b></p>
        """), 500


# =========================================================
# LOT 001 — READ-ONLY PUBLISH READINESS CHECK
# Checks Offer listingDuration and category condition policy.
# GET only. No PUT/POST to eBay. NEVER calls publishOffer.
# =========================================================

@app.route('/ebay/lot001-publish-readiness')
def ebay_lot001_publish_readiness():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Language': 'en-US',
        }

        offer_id = '264904072011'
        category_id = '261604'
        marketplace_id = 'EBAY_US'

        offer_url = f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'
        condition_url = (
            f'https://api.ebay.com/sell/metadata/v1/marketplace/'
            f'{marketplace_id}/get_item_condition_policies'
        )

        offer_r = requests.get(offer_url, headers=headers, timeout=30)
        condition_r = requests.get(
            condition_url,
            headers=headers,
            params={'category_ids': category_id},
            timeout=30
        )

        if offer_r.status_code != 200 or condition_r.status_code != 200:
            return page('LOT 001 publish readiness error', f"""
                <h2>LOT 001 — read-only readiness check failed</h2>
                <p><b>Offer GET:</b> HTTP {offer_r.status_code}</p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(offer_r.text[:7000])}</pre>
                <p><b>Condition policy GET:</b> HTTP {condition_r.status_code}</p>
                <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(condition_r.text[:7000])}</pre>
                <p><b>No data was changed. publishOffer was NOT called.</b></p>
            """), 502

        offer = offer_r.json()
        condition_data = condition_r.json()

        listing_duration = offer.get('listingDuration')
        offer_status = offer.get('status')
        sku = offer.get('sku')
        actual_category = str(offer.get('categoryId') or '')
        actual_marketplace = offer.get('marketplaceId')

        policies = condition_data.get('itemConditionPolicies') or []
        target_policy = None
        for policy in policies:
            if str(policy.get('categoryId') or '') == category_id:
                target_policy = policy
                break

        if target_policy is None and len(policies) == 1:
            target_policy = policies[0]

        item_conditions = (target_policy or {}).get('itemConditions') or []
        condition_rows = []
        condition_7000 = None

        for item in item_conditions:
            cid = str(item.get('conditionId') or '')
            cdesc = str(item.get('conditionDescription') or '')
            if cid == '7000':
                condition_7000 = item
            condition_rows.append(
                '<tr>'
                f'<td>{html.escape(cid)}</td>'
                f'<td>{html.escape(cdesc)}</td>'
                f'<td>{"YES" if cid == "7000" else ""}</td>'
                '</tr>'
            )

        duration_ok = str(listing_duration or '').upper() == 'GTC'
        condition_ok = condition_7000 is not None
        offer_identity_ok = (
            offer_status == 'UNPUBLISHED'
            and sku == 'LOT-001'
            and actual_category == category_id
            and actual_marketplace == marketplace_id
        )

        overall_ok = duration_ok and condition_ok and offer_identity_ok

        status_text = (
            'READY ON THESE CHECKS — GTC and condition policy are valid.'
            if overall_ok
            else 'ACTION REQUIRED BEFORE PUBLISH.'
        )

        duration_action = (
            'PASS — GTC is already set.'
            if duration_ok
            else 'MISSING/NOT GTC — do not publish yet; update Offer safely first.'
        )

        condition_action = (
            'PASS — Condition ID 7000 is supported in category 261604.'
            if condition_ok
            else 'STOP — Condition ID 7000 was NOT returned for category 261604.'
        )

        return page('LOT 001 — publish readiness', f"""
            <h2>LOT 001 — publish readiness</h2>
            <h3>{html.escape(status_text)}</h3>
            <p><b>This page is READ-ONLY.</b> It performed GET requests only.</p>

            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Check</th><th>Actual</th><th>Result</th></tr>
                <tr>
                    <td>Offer identity/status</td>
                    <td>Offer {html.escape(str(offer_id))}, SKU {html.escape(str(sku))},
                        {html.escape(str(actual_marketplace))}, category {html.escape(str(actual_category))},
                        status {html.escape(str(offer_status))}</td>
                    <td>{"PASS" if offer_identity_ok else "FAIL"}</td>
                </tr>
                <tr>
                    <td>listingDuration</td>
                    <td>{html.escape(str(listing_duration))}</td>
                    <td>{html.escape(duration_action)}</td>
                </tr>
                <tr>
                    <td>FOR_PARTS_OR_NOT_WORKING / Condition ID 7000</td>
                    <td>{html.escape(str((condition_7000 or {}).get('conditionDescription') or 'NOT RETURNED'))}</td>
                    <td>{html.escape(condition_action)}</td>
                </tr>
            </table>

            <h3>Conditions returned by eBay for category 261604</h3>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Condition ID</th><th>Description</th><th>Our condition</th></tr>
                {''.join(condition_rows)}
            </table>

            <hr>
            <p><b>Offer GET:</b> HTTP 200</p>
            <p><b>Metadata condition-policy GET:</b> HTTP 200</p>
            <p><b>publishOffer was NOT called. Nothing was published live.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 publish readiness error', f"""
            <h2>LOT 001 — readiness error</h2>
            <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
            <p><b>No data was changed. publishOffer was NOT called.</b></p>
        """), 500


# =========================================================
# LOT 001 — FINAL SAFE PUBLISH
# GET = authoritative confirmation only.
# POST = re-checks all critical fields, then calls publishOffer once.
# =========================================================

@app.route('/ebay/publish-lot001', methods=['GET','POST'])
def ebay_publish_lot001():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Language': 'en-US',
        }

        offer_id='264904072011'
        sku='LOT-001'
        category_id='261604'
        marketplace='EBAY_US'

        offer_url=f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'
        inv_url=f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
        cond_url=f'https://api.ebay.com/sell/metadata/v1/marketplace/{marketplace}/get_item_condition_policies'

        def get_authoritative():
            orr=requests.get(offer_url,headers=headers,timeout=30)
            irr=requests.get(inv_url,headers=headers,timeout=30)
            crr=requests.get(cond_url,headers=headers,params={'category_ids':category_id},timeout=30)
            return orr,irr,crr

        orr,irr,crr=get_authoritative()
        if orr.status_code!=200 or irr.status_code!=200 or crr.status_code!=200:
            return page('LOT 001 publish safety stop',f"""
              <h2>Safety stop — authoritative read failed</h2>
              <p>Offer GET: HTTP {orr.status_code}</p>
              <p>Inventory GET: HTTP {irr.status_code}</p>
              <p>Condition-policy GET: HTTP {crr.status_code}</p>
              <p><b>publishOffer was NOT called.</b></p>
            """),502

        offer=orr.json(); inv=irr.json(); cdata=crr.json()
        product=inv.get('product') or {}
        images=product.get('imageUrls') or []
        aspects=product.get('aspects') or {}
        policies=offer.get('listingPolicies') or {}
        price=((offer.get('pricingSummary') or {}).get('price') or {})

        cond7000=False
        for pol in cdata.get('itemConditionPolicies') or []:
            if str(pol.get('categoryId') or '')==category_id:
                cond7000=any(str(x.get('conditionId') or '')=='7000' for x in (pol.get('itemConditions') or []))

        checks=[
          ('Offer status',offer.get('status'),'UNPUBLISHED',offer.get('status')=='UNPUBLISHED'),
          ('SKU',offer.get('sku'),sku,offer.get('sku')==sku),
          ('Marketplace',offer.get('marketplaceId'),marketplace,offer.get('marketplaceId')==marketplace),
          ('Category',str(offer.get('categoryId') or ''),category_id,str(offer.get('categoryId') or '')==category_id),
          ('Format',offer.get('format'),'FIXED_PRICE',offer.get('format')=='FIXED_PRICE'),
          ('Duration',offer.get('listingDuration'),'GTC',offer.get('listingDuration')=='GTC'),
          ('Quantity',offer.get('availableQuantity'),1,offer.get('availableQuantity')==1),
          ('Price',price.get('value'),'49.99',str(price.get('value')) in ('49.99','49.990','49.9900')),
          ('Currency',price.get('currency'),'USD',price.get('currency')=='USD'),
          ('Location',offer.get('merchantLocationKey'),'vintage-ua-warehouse-1',offer.get('merchantLocationKey')=='vintage-ua-warehouse-1'),
          ('Payment policy',policies.get('paymentPolicyId'),'275629409012',policies.get('paymentPolicyId')=='275629409012'),
          ('Return policy',policies.get('returnPolicyId'),'275629453012',policies.get('returnPolicyId')=='275629453012'),
          ('Fulfillment policy',policies.get('fulfillmentPolicyId'),'275629516012',policies.get('fulfillmentPolicyId')=='275629516012'),
          ('Condition',inv.get('condition'),'FOR_PARTS_OR_NOT_WORKING',inv.get('condition')=='FOR_PARTS_OR_NOT_WORKING'),
          ('Condition 7000 allowed','YES','YES',cond7000),
          ('EPS images',len(images),24,len(images)==24),
          ('Brand',', '.join(aspects.get('Brand') or []),'Mayak',bool(aspects.get('Brand'))),
          ('Country of Origin removed','YES' if 'Country of Origin' not in aspects else 'NO','YES','Country of Origin' not in aspects),
          ('Unconfirmed decade removed','YES' if 'Time Period Manufactured' not in aspects else 'NO','YES','Time Period Manufactured' not in aspects),
        ]
        all_ok=all(x[3] for x in checks)

        rows=''.join(
          '<tr><td>'+('PASS' if ok else 'FAIL')+'</td><td>'+html.escape(str(n))+
          '</td><td>'+html.escape(str(a))+'</td><td>'+html.escape(str(e))+'</td></tr>'
          for n,a,e,ok in checks
        )

        if not all_ok:
            return page('LOT 001 publish safety stop',f"""
              <h2>LOT 001 — NOT READY TO PUBLISH</h2>
              <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}
              </table>
              <p><b>publishOffer was NOT called.</b></p>
            """),409

        if request.method=='GET':
            return page('LOT 001 — final publish confirmation',f"""
              <h2>LOT 001 — FINAL PUBLISH CONFIRMATION</h2>
              <p><b>All authoritative safety checks passed.</b></p>
              <table border="1" cellpadding="5" cellspacing="0">
                <tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}
              </table>
              <h3>This next button WILL publish the listing live on eBay.com.</h3>
              <p>Offer ID: {offer_id} &nbsp; | &nbsp; SKU: {sku}</p>
              <p>Price: $49.99 USD &nbsp; | &nbsp; Quantity: 1 &nbsp; | &nbsp; Shipping policy: $49.99 to USA</p>
              <form method="post">
                <button type="submit" style="padding:14px 20px;font-size:17px;font-weight:bold;">
                  PUBLISH LOT-001 LIVE ON EBAY
                </button>
              </form>
            """)

        # POST: immediately re-read everything so a stale confirmation page cannot publish changed data.
        orr2,irr2,crr2=get_authoritative()
        if orr2.status_code!=200 or irr2.status_code!=200 or crr2.status_code!=200:
            return page('LOT 001 publish safety stop',"""
              <h2>Final re-check failed.</h2>
              <p><b>publishOffer was NOT called.</b></p>
            """),502

        o2=orr2.json(); i2=irr2.json()
        p2=i2.get('product') or {}
        a2=p2.get('aspects') or {}
        lp2=o2.get('listingPolicies') or {}
        pr2=((o2.get('pricingSummary') or {}).get('price') or {})
        c2=False
        for pol in (crr2.json().get('itemConditionPolicies') or []):
            if str(pol.get('categoryId') or '')==category_id:
                c2=any(str(x.get('conditionId') or '')=='7000' for x in (pol.get('itemConditions') or []))

        final_ok=(
          o2.get('status')=='UNPUBLISHED' and o2.get('sku')==sku
          and o2.get('marketplaceId')==marketplace
          and str(o2.get('categoryId') or '')==category_id
          and o2.get('format')=='FIXED_PRICE'
          and o2.get('listingDuration')=='GTC'
          and o2.get('availableQuantity')==1
          and str(pr2.get('value')) in ('49.99','49.990','49.9900')
          and pr2.get('currency')=='USD'
          and o2.get('merchantLocationKey')=='vintage-ua-warehouse-1'
          and lp2.get('paymentPolicyId')=='275629409012'
          and lp2.get('returnPolicyId')=='275629453012'
          and lp2.get('fulfillmentPolicyId')=='275629516012'
          and i2.get('condition')=='FOR_PARTS_OR_NOT_WORKING'
          and len(p2.get('imageUrls') or [])==24
          and bool(a2.get('Brand'))
          and 'Country of Origin' not in a2
          and 'Time Period Manufactured' not in a2
          and c2
        )
        if not final_ok:
            return page('LOT 001 publish safety stop',"""
              <h2>Data changed between confirmation and publish.</h2>
              <p><b>Safety stop. publishOffer was NOT called.</b></p>
            """),409

        publish_url=f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish'
        pub=requests.post(publish_url,headers={**headers,'Content-Type':'application/json'},timeout=45)

        if pub.status_code not in (200,201):
            return page('LOT 001 publish failed',f"""
              <h2>eBay did not publish LOT-001</h2>
              <p><b>publishOffer HTTP {pub.status_code}</b></p>
              <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(pub.text[:10000])}</pre>
              <p>Do not press Publish again until this response is reviewed.</p>
            """),502

        try:
            pdata=pub.json()
        except Exception:
            pdata={}

        listing_id=pdata.get('listingId') or ''
        verify=requests.get(offer_url,headers=headers,timeout=30)
        verified_status=''
        if verify.status_code==200:
            verified_status=str((verify.json() or {}).get('status') or '')

        return page('LOT 001 — PUBLISHED',f"""
          <h2>LOT 001 — eBay publish request succeeded</h2>
          <p><b>publishOffer HTTP:</b> {pub.status_code}</p>
          <p><b>Listing ID:</b> {html.escape(str(listing_id))}</p>
          <p><b>Offer verification GET:</b> HTTP {verify.status_code}</p>
          <p><b>Offer status after publish:</b> {html.escape(verified_status)}</p>
          <p><b>Do not press Publish again.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 publish error',f"""
          <h2>LOT 001 — publish error</h2>
          <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
          <p>Do not retry until the result is reviewed.</p>
        """),500


# =========================================================
# LOT 001 — READ-ONLY INVENTORY LOCATION CHECK
# =========================================================
@app.route('/ebay/lot001-location-check', methods=['GET'])
def ebay_lot001_location_check():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
        }
        key = 'vintage-ua-warehouse-1'
        url = f'https://api.ebay.com/sell/inventory/v1/location/{key}'
        r = requests.get(url, headers=headers, timeout=30)

        if r.status_code != 200:
            return page('LOT 001 — location check', f"""
              <h2>Inventory Location GET failed</h2>
              <p>HTTP {r.status_code}</p>
              <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(r.text[:10000])}</pre>
              <p><b>READ-ONLY. Nothing was changed or published.</b></p>
            """), 502

        d = r.json() or {}
        loc = d.get('location') or {}
        addr = loc.get('address') or {}

        postal = addr.get('postalCode')
        country = addr.get('country')
        city = addr.get('city')
        state = addr.get('stateOrProvince')

        route_a = bool(postal and country)
        route_b = bool(city and state and country)
        publish_location_ok = route_a or route_b

        def show(v):
            return html.escape(str(v)) if v not in (None, '') else '<b>MISSING</b>'

        return page('LOT 001 — inventory location check', f"""
          <h2>LOT 001 — INVENTORY LOCATION CHECK</h2>
          <p><b>This page is READ-ONLY.</b> It performed one GET request only.</p>
          <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Field</th><th>Value stored by eBay</th></tr>
            <tr><td>merchantLocationKey</td><td>{show(d.get('merchantLocationKey') or key)}</td></tr>
            <tr><td>name</td><td>{show(d.get('name'))}</td></tr>
            <tr><td>merchantLocationStatus</td><td>{show(d.get('merchantLocationStatus'))}</td></tr>
            <tr><td>locationTypes</td><td>{show(d.get('locationTypes'))}</td></tr>
            <tr><td>postalCode</td><td>{show(postal)}</td></tr>
            <tr><td>country</td><td>{show(country)}</td></tr>
            <tr><td>city</td><td>{show(city)}</td></tr>
            <tr><td>stateOrProvince</td><td>{show(state)}</td></tr>
          </table>
          <h3>Publish-location requirement</h3>
          <p>postalCode + country: <b>{'PASS' if route_a else 'FAIL'}</b></p>
          <p>city + stateOrProvince + country: <b>{'PASS' if route_b else 'FAIL'}</b></p>
          <p>Overall address requirement: <b>{'PASS' if publish_location_ok else 'FAIL'}</b></p>
          <p>Inventory Location GET: HTTP {r.status_code}</p>
          <p><b>No PUT/POST was sent. Nothing was changed. publishOffer was NOT called.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 — location check error', f"""
          <h2>Location check error</h2>
          <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
          <p><b>Nothing was changed or published.</b></p>
        """), 500


# =========================================================
# LOT 001 — SAFE INVENTORY LOCATION UPDATE
# GET: preview only. POST: update same warehouse, then verify.
# DOES NOT publish any offer.
# =========================================================
@app.route('/ebay/lot001-location-update', methods=['GET','POST'])
def ebay_lot001_location_update():
    try:
        access_token = get_ebay_access_token()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        key = 'vintage-ua-warehouse-1'
        get_url = f'https://api.ebay.com/sell/inventory/v1/location/{key}'
        update_url = f'https://api.ebay.com/sell/inventory/v1/location/{key}/update_location_details'

        before_r = requests.get(get_url, headers=headers, timeout=30)
        if before_r.status_code != 200:
            return page('LOT 001 location update — stop', f"""
              <h2>Safety stop — could not read current location</h2>
              <p>GET HTTP {before_r.status_code}</p>
              <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(before_r.text[:10000])}</pre>
              <p><b>Nothing was changed. publishOffer was NOT called.</b></p>
            """), 502

        before = before_r.json() or {}
        old_addr = ((before.get('location') or {}).get('address') or {})

        new_addr = {
            'city': 'Sumy',
            'stateOrProvince': 'Sumy Oblast',
            'postalCode': '40016',
            'country': 'UA',
        }

        def esc(v):
            return html.escape(str(v if v not in (None, '') else 'MISSING'))

        if request.method == 'GET':
            return page('LOT 001 — confirm location update', f"""
              <h2>LOT 001 — CONFIRM INVENTORY LOCATION UPDATE</h2>
              <p><b>This page has NOT changed anything yet.</b></p>
              <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Field</th><th>Current eBay value</th><th>New value</th></tr>
                <tr><td>merchantLocationKey</td><td>{esc(key)}</td><td>{esc(key)} (unchanged)</td></tr>
                <tr><td>city</td><td>{esc(old_addr.get('city'))}</td><td>Sumy</td></tr>
                <tr><td>stateOrProvince</td><td>{esc(old_addr.get('stateOrProvince'))}</td><td>Sumy Oblast</td></tr>
                <tr><td>postalCode</td><td>{esc(old_addr.get('postalCode'))}</td><td>40016</td></tr>
                <tr><td>country</td><td>{esc(old_addr.get('country'))}</td><td>UA</td></tr>
              </table>
              <p>The existing warehouse is updated; no new inventory location is created.</p>
              <p><b>This button updates ONLY the inventory location. It does NOT publish LOT-001.</b></p>
              <form method="post">
                <button type="submit" style="padding:14px 20px;font-size:17px;font-weight:bold;">
                  UPDATE LOCATION TO SUMY 40016
                </button>
              </form>
            """)

        # POST: re-read and ensure we are modifying the intended enabled warehouse.
        check_r = requests.get(get_url, headers=headers, timeout=30)
        if check_r.status_code != 200:
            return page('LOT 001 location update — stop', """
              <h2>Final safety read failed.</h2>
              <p><b>Nothing was changed. publishOffer was NOT called.</b></p>
            """), 502

        check = check_r.json() or {}
        types = check.get('locationTypes') or []
        if check.get('merchantLocationStatus') != 'ENABLED' or 'WAREHOUSE' not in types:
            return page('LOT 001 location update — stop', f"""
              <h2>Safety stop — unexpected inventory location state</h2>
              <p>Status: {esc(check.get('merchantLocationStatus'))}</p>
              <p>Types: {esc(types)}</p>
              <p><b>Nothing was changed. publishOffer was NOT called.</b></p>
            """), 409

        payload = {'location': {'address': new_addr}}
        upd = requests.post(update_url, headers=headers, json=payload, timeout=30)

        if upd.status_code not in (200, 204):
            return page('LOT 001 location update failed', f"""
              <h2>eBay did not update the inventory location</h2>
              <p>Update HTTP {upd.status_code}</p>
              <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(upd.text[:10000])}</pre>
              <p><b>publishOffer was NOT called.</b></p>
            """), 502

        verify_r = requests.get(get_url, headers=headers, timeout=30)
        if verify_r.status_code != 200:
            return page('LOT 001 location verification failed', f"""
              <h2>Update request succeeded, but verification GET failed</h2>
              <p>Update HTTP {upd.status_code}</p>
              <p>Verification GET HTTP {verify_r.status_code}</p>
              <p><b>Do NOT publish yet. publishOffer was NOT called.</b></p>
            """), 502

        after = verify_r.json() or {}
        addr = ((after.get('location') or {}).get('address') or {})
        verified = (
            addr.get('city') == 'Sumy'
            and addr.get('stateOrProvince') == 'Sumy Oblast'
            and addr.get('postalCode') == '40016'
            and addr.get('country') == 'UA'
        )

        return page('LOT 001 — location update result', f"""
          <h2>LOT 001 — INVENTORY LOCATION UPDATE RESULT</h2>
          <p>Update HTTP: <b>{upd.status_code}</b></p>
          <p>Verification GET HTTP: <b>{verify_r.status_code}</b></p>
          <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Field</th><th>Value now stored by eBay</th></tr>
            <tr><td>city</td><td>{esc(addr.get('city'))}</td></tr>
            <tr><td>stateOrProvince</td><td>{esc(addr.get('stateOrProvince'))}</td></tr>
            <tr><td>postalCode</td><td>{esc(addr.get('postalCode'))}</td></tr>
            <tr><td>country</td><td>{esc(addr.get('country'))}</td></tr>
          </table>
          <p><b>Verification: {'PASS' if verified else 'FAIL'}</b></p>
          <p><b>publishOffer was NOT called. LOT-001 was NOT published by this page.</b></p>
        """)

    except Exception as exc:
        return page('LOT 001 location update error', f"""
          <h2>Location update error</h2>
          <pre style="white-space:pre-wrap;word-break:break-word;">{html.escape(str(exc))}</pre>
          <p><b>publishOffer was NOT called.</b></p>
        """), 500


# LOT001 post-end control: READ ONLY
@app.route('/ebay/lot001-post-end-control', methods=['GET'])
def ebay_lot001_post_end_control():
    try:
        token=get_ebay_access_token()
        h={'Authorization':f'Bearer {token}','Accept':'application/json'}
        sku='LOT-001'; offer_id='264904072011'
        ir=requests.get(f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}',headers=h,timeout=30)
        orr=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=h,timeout=30)
        if ir.status_code!=200 or orr.status_code!=200:
            return page('LOT001 control',f"""<h2>Read failed</h2>
            <p>Inventory GET HTTP {ir.status_code}</p><p>Offer GET HTTP {orr.status_code}</p>
            <p><b>READ-ONLY. Nothing changed or published.</b></p>"""),502
        inv=ir.json() or {}; offer=orr.json() or {}
        product=inv.get('product') or {}; imgs=product.get('imageUrls') or []; aspects=product.get('aspects') or {}
        def e(x): return html.escape(str(x))
        thumbs=''.join(f"""<div style="display:inline-block;width:145px;vertical-align:top;margin:6px">
          <div><b>Photo {i}</b>{' — CURRENT MAIN' if i==1 else ''}</div>
          <img src="{html.escape(u)}" style="width:135px;height:135px;object-fit:contain;border:1px solid #bbb">
        </div>""" for i,u in enumerate(imgs,1))
        aspect_rows=''.join(f'<tr><td>{e(k)}</td><td>{e(v)}</td></tr>' for k,v in sorted(aspects.items()))
        return page('LOT001 post-end control',f"""
          <h2>LOT 001 — POST-END CONTROL (READ ONLY)</h2>
          <p><b>Inventory GET:</b> HTTP {ir.status_code} &nbsp; <b>Offer GET:</b> HTTP {orr.status_code}</p>
          <table border="1" cellpadding="6" cellspacing="0">
           <tr><th>Field</th><th>Actual eBay API value</th></tr>
           <tr><td>SKU</td><td>{e(offer.get('sku'))}</td></tr>
           <tr><td>Offer ID</td><td>{offer_id}</td></tr>
           <tr><td>Offer status</td><td>{e(offer.get('status'))}</td></tr>
           <tr><td>Listing ID (if returned)</td><td>{e(offer.get('listingId'))}</td></tr>
           <tr><td>Inventory condition</td><td>{e(inv.get('condition'))}</td></tr>
           <tr><td>Image count</td><td>{len(imgs)}</td></tr>
           <tr><td>Country of Origin aspect</td><td>{e(aspects.get('Country of Origin','MISSING'))}</td></tr>
           <tr><td>Time Period Manufactured</td><td>{e(aspects.get('Time Period Manufactured','MISSING'))}</td></tr>
          </table>
          <h3>All Inventory Item aspects returned by eBay</h3>
          <table border="1" cellpadding="5" cellspacing="0"><tr><th>Aspect</th><th>Value</th></tr>{aspect_rows}</table>
          <h3>24 eBay EPS images in their CURRENT API order</h3>
          <p>Photo 1 is the image eBay receives first and therefore the current Main candidate.</p>
          <div>{thumbs}</div>
          <p><b>READ-ONLY: no PUT, POST, delete, revise, createOffer, or publishOffer was called.</b></p>
        """)
    except Exception as exc:
        return page('LOT001 control error',f"""<h2>Error</h2><pre>{html.escape(str(exc))}</pre>
        <p><b>Nothing changed or published.</b></p>"""),500


@app.route('/ebay/lot001-set-main-photo23', methods=['GET','POST'])
def ebay_lot001_set_main_photo23():
    try:
        token = get_ebay_access_token()
        h = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        sku = 'LOT-001'
        url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
        r = requests.get(url, headers=h, timeout=30)
        if r.status_code != 200:
            return page('LOT001 photo fix', '<h2>Inventory GET failed: HTTP %s</h2><pre>%s</pre>' % (r.status_code, html.escape(r.text))), 502

        inv = r.json() or {}
        product = inv.get('product') or {}
        imgs = list(product.get('imageUrls') or [])
        if len(imgs) != 24:
            return page('LOT001 photo fix', '<h2>SAFETY STOP</h2><p>Expected 24 images, eBay returned %s. Nothing changed.</p>' % len(imgs)), 409

        newimgs = [imgs[22]] + imgs[:22] + imgs[23:]

        if request.method == 'GET':
            body = '''
            <h2>LOT 001 — SET PHOTO 23 AS MAIN</h2>
            <p>Current image count: <b>24</b></p>
            <p>Photo 23 will move to position 1. All other 23 images will be preserved.</p>
            <div style="display:flex;gap:25px">
              <div><b>Current Main — Photo 1</b><br><img src="%s" style="max-width:280px;max-height:280px"></div>
              <div><b>New Main — Photo 23</b><br><img src="%s" style="max-width:280px;max-height:280px"></div>
            </div>
            <p><b>This GET page has changed nothing.</b></p>
            <form method="post"><button style="padding:14px;font-size:17px">CONFIRM: MOVE PHOTO 23 TO MAIN</button></form>
            ''' % (html.escape(imgs[0]), html.escape(imgs[22]))
            return page('LOT001 photo fix', body)

        payload = {}
        for k in ('availability','condition','conditionDescription','packageWeightAndSize'):
            if inv.get(k) is not None:
                payload[k] = inv.get(k)
        payload['product'] = dict(product)
        payload['product']['imageUrls'] = newimgs

        put_h = dict(h)
        put_h['Content-Type'] = 'application/json'
        put_h['Content-Language'] = 'en-US'
        pr = requests.put(url, headers=put_h, json=payload, timeout=45)
        if pr.status_code not in (200, 204):
            return page('LOT001 photo fix', '<h2>PUT failed: HTTP %s</h2><pre>%s</pre><p>publishOffer was NOT called.</p>' % (pr.status_code, html.escape(pr.text))), 502

        vr = requests.get(url, headers=h, timeout=30)
        vinv = vr.json() if vr.status_code == 200 else {}
        vimgs = ((vinv.get('product') or {}).get('imageUrls') or [])
        ok = (len(vimgs) == 24 and vimgs[0] == imgs[22])
        img_html = '<img src="%s" style="max-width:320px;max-height:320px">' % html.escape(vimgs[0]) if vimgs else ''
        body = '''
        <h2>LOT 001 — MAIN PHOTO UPDATE</h2>
        <p>Inventory PUT HTTP: <b>%s</b></p>
        <p>Verification GET HTTP: <b>%s</b></p>
        <p>EPS photos preserved: <b>%s / 24</b></p>
        <p>Photo 23 moved to position 1: <b>%s</b></p>
        %s
        <p><b>publishOffer was NOT called. Nothing was published live by this page.</b></p>
        ''' % (pr.status_code, vr.status_code, len(vimgs), 'PASS' if ok else 'FAIL', img_html)
        return page('LOT001 photo fix', body)
    except Exception as exc:
        return page('LOT001 photo fix error', '<h2>Error</h2><pre>%s</pre><p>publishOffer was NOT called.</p>' % html.escape(str(exc))), 500


@app.route('/ebay/lot001-final-recheck', methods=['GET'])
def ebay_lot001_final_recheck():
    try:
        token=get_ebay_access_token()
        h={'Authorization':f'Bearer {token}','Accept':'application/json'}
        sku='LOT-001'; offer_id='264904072011'; loc_key='vintage-ua-warehouse-1'
        ir=requests.get(f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}',headers=h,timeout=30)
        orr=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=h,timeout=30)
        lr=requests.get(f'https://api.ebay.com/sell/inventory/v1/location/{loc_key}',headers=h,timeout=30)
        if ir.status_code!=200 or orr.status_code!=200 or lr.status_code!=200:
            body='<h2>READ FAILURE</h2><p>Inventory GET HTTP: %s</p><p>Offer GET HTTP: %s</p><p>Location GET HTTP: %s</p><p><b>READ ONLY. Nothing changed or published.</b></p>' % (ir.status_code,orr.status_code,lr.status_code)
            return page('LOT001 final recheck',body),502

        inv=ir.json() or {}; offer=orr.json() or {}; loc=lr.json() or {}
        product=inv.get('product') or {}; imgs=product.get('imageUrls') or []
        aspects=product.get('aspects') or {}
        policies=offer.get('listingPolicies') or {}
        price=(offer.get('pricingSummary') or {}).get('price') or {}
        address=((loc.get('location') or {}).get('address') or {})

        checks=[
          ('Offer status UNPUBLISHED',offer.get('status')=='UNPUBLISHED',offer.get('status')),
          ('SKU LOT-001',offer.get('sku')==sku,offer.get('sku')),
          ('Marketplace EBAY_US',offer.get('marketplaceId')=='EBAY_US',offer.get('marketplaceId')),
          ('Category 261604',str(offer.get('categoryId'))=='261604',offer.get('categoryId')),
          ('Format FIXED_PRICE',offer.get('format')=='FIXED_PRICE',offer.get('format')),
          ('Duration GTC',offer.get('listingDuration')=='GTC',offer.get('listingDuration')),
          ('Quantity 1',offer.get('availableQuantity')==1,offer.get('availableQuantity')),
          ('Price 49.99',str(price.get('value'))=='49.99',price.get('value')),
          ('Currency USD',price.get('currency')=='USD',price.get('currency')),
          ('Location key',offer.get('merchantLocationKey')==loc_key,offer.get('merchantLocationKey')),
          ('Location city Sumy',address.get('city')=='Sumy',address.get('city')),
          ('Location postal 40016',address.get('postalCode')=='40016',address.get('postalCode')),
          ('Location country UA',address.get('country')=='UA',address.get('country')),
          ('Payment policy',policies.get('paymentPolicyId')=='275629409012',policies.get('paymentPolicyId')),
          ('Return policy',policies.get('returnPolicyId')=='275629453012',policies.get('returnPolicyId')),
          ('Fulfillment policy',policies.get('fulfillmentPolicyId')=='275629516012',policies.get('fulfillmentPolicyId')),
          ('Condition FOR_PARTS_OR_NOT_WORKING',inv.get('condition')=='FOR_PARTS_OR_NOT_WORKING',inv.get('condition')),
          ('EPS images 24',len(imgs)==24,len(imgs)),
          ('Brand Mayak',aspects.get('Brand')==['Mayak'],aspects.get('Brand')),
          ('Country aspect absent','Country of Origin' not in aspects,aspects.get('Country of Origin','MISSING')),
          ('Unconfirmed decade absent','Time Period Manufactured' not in aspects,aspects.get('Time Period Manufactured','MISSING'))
        ]
        all_ok=all(x[1] for x in checks)
        rows=''.join('<tr><td>%s</td><td><b>%s</b></td><td>%s</td></tr>' % (html.escape(str(n)),'PASS' if ok else 'FAIL',html.escape(str(v))) for n,ok,v in checks)
        main=html.escape(imgs[0]) if imgs else ''
        img_html='<img src="%s" style="max-width:320px;max-height:320px">' % main if main else '<p>No image</p>'
        body='<h2>LOT 001 — FINAL RECHECK (READ ONLY)</h2><p><b>Overall: %s</b></p><p>Inventory GET HTTP %s | Offer GET HTTP %s | Location GET HTTP %s</p><table border="1" cellpadding="5" cellspacing="0"><tr><th>Check</th><th>Result</th><th>Actual</th></tr>%s</table><h3>Current Main Photo (API position 1)</h3>%s<p><b>READ ONLY: no PUT, POST, revise, createOffer, or publishOffer was called.</b></p>' % ('ALL CHECKS PASS' if all_ok else 'SAFETY STOP — CHECK FAILED',ir.status_code,orr.status_code,lr.status_code,rows,img_html)
        return page('LOT001 final recheck',body)
    except Exception as exc:
        return page('LOT001 final recheck error','<h2>Error</h2><pre>%s</pre><p>Nothing changed or published.</p>' % html.escape(str(exc))),500


@app.route('/ebay/republish-lot001', methods=['GET','POST'])
def ebay_republish_lot001():
    try:
        token=get_ebay_access_token()
        h={'Authorization':f'Bearer {token}','Accept':'application/json'}
        sku='LOT-001'; offer_id='264904072011'; loc_key='vintage-ua-warehouse-1'
        def snapshot():
            ir=requests.get(f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}',headers=h,timeout=30)
            orr=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=h,timeout=30)
            lr=requests.get(f'https://api.ebay.com/sell/inventory/v1/location/{loc_key}',headers=h,timeout=30)
            if ir.status_code!=200 or orr.status_code!=200 or lr.status_code!=200:
                return None,(ir.status_code,orr.status_code,lr.status_code)
            inv=ir.json() or {}; offer=orr.json() or {}; loc=lr.json() or {}
            product=inv.get('product') or {}; imgs=product.get('imageUrls') or []; aspects=product.get('aspects') or {}
            policies=offer.get('listingPolicies') or {}; price=(offer.get('pricingSummary') or {}).get('price') or {}
            address=((loc.get('location') or {}).get('address') or {})
            checks=[
              ('Offer status UNPUBLISHED',offer.get('status')=='UNPUBLISHED',offer.get('status')),
              ('SKU LOT-001',offer.get('sku')==sku,offer.get('sku')),
              ('Marketplace EBAY_US',offer.get('marketplaceId')=='EBAY_US',offer.get('marketplaceId')),
              ('Category 261604',str(offer.get('categoryId'))=='261604',offer.get('categoryId')),
              ('Format FIXED_PRICE',offer.get('format')=='FIXED_PRICE',offer.get('format')),
              ('Duration GTC',offer.get('listingDuration')=='GTC',offer.get('listingDuration')),
              ('Quantity 1',offer.get('availableQuantity')==1,offer.get('availableQuantity')),
              ('Price 49.99',str(price.get('value'))=='49.99',price.get('value')),
              ('Currency USD',price.get('currency')=='USD',price.get('currency')),
              ('Location key',offer.get('merchantLocationKey')==loc_key,offer.get('merchantLocationKey')),
              ('Location city Sumy',address.get('city')=='Sumy',address.get('city')),
              ('Location postal 40016',address.get('postalCode')=='40016',address.get('postalCode')),
              ('Location country UA',address.get('country')=='UA',address.get('country')),
              ('Payment policy',policies.get('paymentPolicyId')=='275629409012',policies.get('paymentPolicyId')),
              ('Return policy',policies.get('returnPolicyId')=='275629453012',policies.get('returnPolicyId')),
              ('Fulfillment policy',policies.get('fulfillmentPolicyId')=='275629516012',policies.get('fulfillmentPolicyId')),
              ('Condition',inv.get('condition')=='FOR_PARTS_OR_NOT_WORKING',inv.get('condition')),
              ('EPS images 24',len(imgs)==24,len(imgs)),
              ('Brand Mayak',aspects.get('Brand')==['Mayak'],aspects.get('Brand')),
              ('Country aspect absent','Country of Origin' not in aspects,aspects.get('Country of Origin','MISSING')),
              ('Unconfirmed decade absent','Time Period Manufactured' not in aspects,aspects.get('Time Period Manufactured','MISSING'))
            ]
            return {'checks':checks,'imgs':imgs,'offer':offer},None

        snap,err=snapshot()
        if err:
            return page('LOT001 republish','<h2>READ FAILURE</h2><p>Inventory/Offer/Location HTTP: %s / %s / %s</p><p>Nothing published.</p>'%err),502
        ok=all(x[1] for x in snap['checks'])
        rows=''.join('<tr><td>%s</td><td><b>%s</b></td><td>%s</td></tr>'%(html.escape(str(n)),'PASS' if v else 'FAIL',html.escape(str(a))) for n,v,a in snap['checks'])
        main=html.escape(snap['imgs'][0]) if snap['imgs'] else ''
        if request.method=='GET':
            button='<form method="post"><button style="padding:16px;font-size:18px" %s>REPUBLISH LOT-001 LIVE ON EBAY</button></form>'%('' if ok else 'disabled')
            body='<h2>LOT 001 — FINAL REPUBLISH CONFIRMATION</h2><p><b>%s</b></p><table border="1" cellpadding="5" cellspacing="0"><tr><th>Check</th><th>Result</th><th>Actual</th></tr>%s</table><h3>Main Photo</h3><img src="%s" style="max-width:300px;max-height:300px"><p><b>GET changed nothing. Nothing is live from this page yet.</b></p>%s'%(('ALL CHECKS PASS' if ok else 'SAFETY STOP'),rows,main,button)
            return page('LOT001 republish',body)
        if not ok:
            return page('LOT001 republish','<h2>SAFETY STOP</h2><p>A required check failed. publishOffer was NOT called.</p>'),409

        # Re-read immediately before the one consequential call.
        snap2,err2=snapshot()
        if err2 or not all(x[1] for x in snap2['checks']):
            return page('LOT001 republish','<h2>SAFETY STOP</h2><p>Authoritative data changed before publish. publishOffer was NOT called.</p>'),409

        ph=dict(h); ph['Content-Type']='application/json'
        pr=requests.post(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',headers=ph,timeout=45)
        if pr.status_code not in (200,201):
            return page('LOT001 republish','<h2>eBay publish failed</h2><p>HTTP: <b>%s</b></p><pre>%s</pre><p><b>Do not press Publish again.</b></p>'%(pr.status_code,html.escape(pr.text))),502
        try: pdata=pr.json() or {}
        except Exception: pdata={}
        listing_id=pdata.get('listingId')
        vr=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=h,timeout=30)
        vo=vr.json() if vr.status_code==200 else {}
        return page('LOT001 republish','<h2>LOT 001 — eBay republish request succeeded</h2><p>publishOffer HTTP: <b>%s</b></p><p>Listing ID: <b>%s</b></p><p>Offer verification GET: HTTP <b>%s</b></p><p>Offer status after publish: <b>%s</b></p><p><b>Do not press Publish again.</b></p>'%(pr.status_code,html.escape(str(listing_id)),vr.status_code,html.escape(str(vo.get('status')))))
    except Exception as exc:
        return page('LOT001 republish error','<h2>Error</h2><pre>%s</pre><p>Do not retry until this result is reviewed.</p>'%html.escape(str(exc))),500


@app.route('/ebay/lot001-shipping-update', methods=['GET','POST'])
def ebay_lot001_shipping_update():
    """Safely update LOT-001 fulfillment policy shipping cost to $46.99."""
    policy_id='275629516012'
    target_cost='46.99'
    try:
        token=get_ebay_access_token()
        h={'Authorization':f'Bearer {token}','Accept':'application/json'}
        url=f'https://api.ebay.com/sell/account/v1/fulfillment_policy/{policy_id}'

        def read_policy():
            r=requests.get(url,headers=h,timeout=30)
            data=r.json() if r.status_code==200 and r.text.strip() else {}
            return r,data

        def domestic_service(data):
            for opt in data.get('shippingOptions') or []:
                if opt.get('optionType')=='DOMESTIC':
                    services=opt.get('shippingServices') or []
                    if services:
                        return services[0]
            return None

        r,pol=read_policy()
        if r.status_code!=200:
            return page('LOT001 shipping update','<h2>READ FAILURE</h2><p>Fulfillment Policy GET HTTP: <b>%s</b></p><pre>%s</pre><p><b>Nothing changed.</b></p>'%(r.status_code,html.escape(r.text))),502
        svc=domestic_service(pol)
        current=((svc or {}).get('shippingCost') or {}).get('value')
        currency=((svc or {}).get('shippingCost') or {}).get('currency')
        safe=(str(pol.get('fulfillmentPolicyId'))==policy_id and pol.get('marketplaceId')=='EBAY_US' and svc is not None and currency=='USD')

        if request.method=='GET':
            button='<form method="post"><button style="padding:16px;font-size:18px" %s>UPDATE EBAY SHIPPING TO $46.99</button></form>'%('' if safe else 'disabled')
            body=(
                '<h2>LOT 001 — SHIPPING COST UPDATE</h2>'
                '<p><b>READ ONLY — nothing has been changed yet.</b></p>'
                '<p>Fulfillment Policy ID: <b>%s</b></p>'
                '<p>Marketplace: <b>%s</b></p>'
                '<p>Current eBay shipping cost: <b>$%s %s</b></p>'
                '<p>New shipping cost: <b>$46.99 USD</b></p>'
                '<p>Listing price remains: <b>$49.99 USD</b></p>'
                '<p>All other fulfillment-policy fields will be preserved.</p>%s'
            )%(html.escape(policy_id),html.escape(str(pol.get('marketplaceId'))),html.escape(str(current)),html.escape(str(currency)),button)
            return page('LOT001 shipping update',body)

        if not safe:
            return page('LOT001 shipping update','<h2>SAFETY STOP</h2><p>Policy identity/service/currency check failed. Nothing changed.</p>'),409

        r2,pol2=read_policy()
        svc2=domestic_service(pol2)
        if r2.status_code!=200 or svc2 is None or pol2.get('marketplaceId')!='EBAY_US':
            return page('LOT001 shipping update','<h2>SAFETY STOP</h2><p>Authoritative policy data changed or could not be re-read. Nothing changed.</p>'),409

        payload=dict(pol2)
        for key in ('fulfillmentPolicyId','href'):
            payload.pop(key,None)
        svc2=domestic_service(payload)
        old_cost=((svc2.get('shippingCost') or {}).get('value'))
        svc2['shippingCost']={'value':target_cost,'currency':'USD'}

        uh=dict(h); uh['Content-Type']='application/json'; uh['Content-Language']='en-US'
        ur=requests.put(url,headers=uh,json=payload,timeout=45)
        if ur.status_code not in (200,204):
            return page('LOT001 shipping update','<h2>eBay shipping update failed</h2><p>HTTP: <b>%s</b></p><pre>%s</pre><p><b>Do not retry yet.</b></p>'%(ur.status_code,html.escape(ur.text))),502

        vr,vpol=read_policy()
        vsvc=domestic_service(vpol)
        new_cost=(((vsvc or {}).get('shippingCost') or {}).get('value'))
        verified=(vr.status_code==200 and vsvc is not None and str(new_cost) in ('46.99','46.990','46.9900') and ((vsvc.get('shippingCost') or {}).get('currency'))=='USD')
        body=(
            '<h2>LOT 001 — SHIPPING UPDATE RESULT</h2>'
            '<p>Update HTTP: <b>%s</b></p>'
            '<p>Verification GET HTTP: <b>%s</b></p>'
            '<p>Old shipping cost: <b>$%s USD</b></p>'
            '<p>Current policy shipping cost: <b>$%s USD</b></p>'
            '<p>Verification: <b>%s</b></p>'
            '<p><b>The listing price was not changed.</b></p>'
            '<p>Open the live listing and verify the buyer-facing shipping amount.</p>'
        )%(ur.status_code,vr.status_code,html.escape(str(old_cost)),html.escape(str(new_cost)),'PASS' if verified else 'FAIL')
        return page('LOT001 shipping update',body), (200 if verified else 502)
    except Exception as exc:
        return page('LOT001 shipping update error','<h2>Error</h2><pre>%s</pre><p>Check the policy before retrying.</p>'%html.escape(str(exc))),500

# =========================================================
# LOT 002 — SAFE EBAY PUBLICATION PIPELINE
# Separate SKU / Offer / Fulfillment Policy. LOT 001 is never modified.
# =========================================================

LOT002_SKU = 'LOT-002-R2'
LOT002_MARKETPLACE = 'EBAY_US'
LOT002_CATEGORY_ID = '38037'  # Collectible Telephones (Pre-1940), verified on ebay.com
LOT002_PRICE = '64.99'
LOT002_TITLE = '1930s USSR Krasnaya Zarya UTA Bakelite Rotary Desk Telephone Untested'
LOT002_LOCATION_KEY = 'vintage-ua-warehouse-1'
LOT002_PAYMENT_POLICY = '275629409012'
LOT002_RETURN_POLICY = '275629453012'


def _lot002_extract_section(text, number, next_number=None):
    if not text:
        return ''
    start = re.search(rf'(?mi)^\s*{number}\s+[^\n]*\n', text)
    if not start:
        return ''
    a = start.end()
    if next_number is None:
        return text[a:].strip()
    nxt = re.search(rf'(?mi)^\s*{next_number}\s+[^\n]*\n', text[a:])
    b = a + nxt.start() if nxt else len(text)
    return text[a:b].strip()


def _lot002_current_draft():
    picker_id = session.get('picker_session_id')
    return LOT002_DRAFT_CACHE.get(picker_id) or {}


def _lot002_headers(token, json_body=False):
    h = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Accept-Language': 'en-US',
        'Content-Language': 'en-US',
    }
    if json_body:
        h['Content-Type'] = 'application/json'
    return h




def _lot002_request(method, url, *, retries=3, retry_5xx=True, **kwargs):
    """Retry only operations that are safe to repeat (GET/PUT).

    This protects the workflow from Render/eBay connection resets without
    risking duplicate Offer publication. POST create/publish calls use their
    own recovery logic instead of blind retries.
    """
    method = method.upper()
    if method not in ('GET', 'PUT'):
        raise ValueError('_lot002_request is only for retry-safe GET/PUT calls')
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            if retry_5xx and r.status_code in (502, 503, 504) and attempt < retries:
                time.sleep(attempt * 1.5)
                continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(attempt * 1.5)
    raise RuntimeError(f'Temporary network error after {retries} attempts: {last_exc}')

def _lot002_get_offers(token):
    r = _lot002_request('GET',
        'https://api.ebay.com/sell/inventory/v1/offer',
        headers=_lot002_headers(token),
        params={'sku': LOT002_SKU, 'marketplace_id': LOT002_MARKETPLACE},
        timeout=30,
    )
    if r.status_code == 404:
        try:
            ids = {str(e.get('errorId')) for e in (r.json().get('errors') or [])}
        except Exception:
            ids = set()
        if '25713' in ids:
            return []
    if r.status_code != 200:
        raise RuntimeError(f'Could not check LOT-002 offers: HTTP {r.status_code}: {r.text[:3000]}')
    return r.json().get('offers') or []


def _lot002_get_or_create_shipping_policy(token, shipping_usd):
    # Never reuse or update LOT 001 policy.
    cost = f'{float(shipping_usd):.2f}'
    name = f'Vintage UA eBay Shipping LOT002 {cost}'
    headers = _lot002_headers(token, True)
    r = _lot002_request('GET',
        'https://api.ebay.com/sell/account/v1/fulfillment_policy',
        headers=_lot002_headers(token),
        params={'marketplace_id': LOT002_MARKETPLACE}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f'Fulfillment-policy check failed: HTTP {r.status_code}: {r.text[:3000]}')
    for p in r.json().get('fulfillmentPolicies') or []:
        if str(p.get('name','')).strip().casefold() == name.casefold():
            pid = str(p.get('fulfillmentPolicyId') or '')
            # Verify the existing same-name policy really has the expected cost.
            options = p.get('shippingOptions') or []
            found = False
            for opt in options:
                for svc in opt.get('shippingServices') or []:
                    amt = svc.get('shippingCost') or {}
                    if str(svc.get('shippingServiceCode')) == 'StandardShippingFromOutsideUS' and str(amt.get('currency')) == 'USD':
                        try:
                            found = abs(float(amt.get('value')) - float(cost)) < 0.001
                        except Exception:
                            found = False
            if not pid or not found:
                raise RuntimeError('A LOT002 shipping policy with this name exists but its service/cost does not match. Safety stop.')
            return pid

    payload = {
        'name': name,
        'marketplaceId': LOT002_MARKETPLACE,
        'categoryTypes': [{'name':'ALL_EXCLUDING_MOTORS_VEHICLES'}],
        'handlingTime': {'value':3,'unit':'DAY'},
        'shippingOptions': [{
            'optionType':'DOMESTIC', 'costType':'FLAT_RATE',
            'shippingServices':[{
                'sortOrder':1,
                'shippingServiceCode':'StandardShippingFromOutsideUS',
                'shippingCost':{'value':cost,'currency':'USD'},
                'additionalShippingCost':{'value':'0.00','currency':'USD'},
                'freeShipping':False,
            }]
        }],
        'globalShipping':False, 'pickupDropOff':False, 'freightShipping':False,
    }
    c = requests.post('https://api.ebay.com/sell/account/v1/fulfillment_policy', headers=headers, json=payload, timeout=45)
    if c.status_code not in (200,201):
        raise RuntimeError(f'LOT002 fulfillment policy creation failed: HTTP {c.status_code}: {c.text[:5000]}')
    data = c.json() if c.text.strip() else {}
    pid = str(data.get('fulfillmentPolicyId') or '')
    if not pid:
        raise RuntimeError('eBay created the LOT002 shipping policy but returned no fulfillmentPolicyId.')
    return pid


def _lot002_upload_edited_photos(token):
    picker_id = session.get('picker_session_id')
    items = PHOTO_CACHE.get(picker_id, [])
    if not session.get('photo_editor_approved'):
        raise RuntimeError('Automatic photo editing has not been approved. Return to the photo editor first.')
    if len(items) != 24:
        raise RuntimeError(f'Exactly 24 LOT002 photos are required for this lot; current cache has {len(items)}.')
    cached = EBAY_MEDIA_CACHE.get(LOT002_SKU) or []
    if len(cached) == 24 and all(x.get('image_url') for x in cached):
        return cached

    main_index = int(session.get('main_photo_index') or 0)
    if not (1 <= main_index <= len(items)):
        raise RuntimeError('MAIN PHOTO has not been selected. Return to Photo Editor and choose it.')
    upload_order = [main_index] + [i for i in range(1, len(items)+1) if i != main_index]
    results=[]
    for position,i in enumerate(upload_order,1):
        item=items[i-1]
        mf=item.get('mediaFile') or {}
        filename=mf.get('filename') or f'LOT-002-photo-{i:02d}.jpg'
        image_bytes=_download_picker_photo(i, edited=True, max_side=2400)
        mr=requests.post(
            'https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file',
            headers={'Authorization':f'Bearer {token}','Accept':'application/json'},
            files={'image':(filename,image_bytes,'image/jpeg')}, timeout=120)
        if mr.status_code != 201:
            raise RuntimeError(f'EPS upload stopped at photo {i}: HTTP {mr.status_code}: {mr.text[:4000]}')
        data=mr.json() if mr.text.strip() else {}
        loc=mr.headers.get('Location','')
        image_id=loc.rstrip('/').split('/')[-1] if loc else ''
        image_url=data.get('maxDimensionImageUrl') or data.get('imageUrl') or ''
        if image_id and not image_url:
            gr=requests.get(f'https://apim.ebay.com/commerce/media/v1_beta/image/{image_id}',headers={'Authorization':f'Bearer {token}','Accept':'application/json'},timeout=30)
            if gr.ok:
                gd=gr.json(); image_url=gd.get('maxDimensionImageUrl') or gd.get('imageUrl') or ''
        if not image_url:
            raise RuntimeError(f'EPS upload {i} succeeded but no usable image URL was returned.')
        results.append({'number':position,'source_number':i,'is_main':position==1,'filename':filename,'image_id':image_id,'image_url':image_url})
    EBAY_MEDIA_CACHE[LOT002_SKU]=results
    return results


def _lot002_condition_used_allowed(token):
    r=requests.get(
        f'https://api.ebay.com/sell/metadata/v1/marketplace/{LOT002_MARKETPLACE}/get_item_condition_policies',
        headers=_lot002_headers(token), params={'category_ids':LOT002_CATEGORY_ID}, timeout=30)
    if r.status_code != 200:
        return False, f'HTTP {r.status_code}: {r.text[:3000]}'
    for pol in r.json().get('itemConditionPolicies') or []:
        if str(pol.get('categoryId') or '') == LOT002_CATEGORY_ID:
            for c in pol.get('itemConditions') or []:
                if str(c.get('conditionId') or '') == '3000':
                    return True, 'USED_EXCELLENT / 3000'
    return False, 'Condition ID 3000 not returned for category'


@app.route('/ebay/lot002-preflight')
def ebay_lot002_preflight():
    draft=_lot002_current_draft()
    picker_id=session.get('picker_session_id')
    items=PHOTO_CACHE.get(picker_id,[])
    if not draft:
        return page('LOT 002 preflight','<h2>LOT 002 draft is not in the current Render memory.</h2><p>Return to the final draft page and create it again.</p>'),400
    try:
        token=get_ebay_access_token()
        used_ok,cond_detail=_lot002_condition_used_allowed(token)
        offers=_lot002_get_offers(token)
        existing='none' if not offers else ', '.join(f"{o.get('offerId')} ({o.get('status')})" for o in offers)
        main_index=int(session.get('main_photo_index') or 0)
        all_ok=(len(items)==24 and bool(session.get('photo_editor_approved')) and 1 <= main_index <= len(items) and used_ok and draft.get('shipping_usd'))
        existing_update=(all_ok and len(offers)==1 and offers[0].get('status')=='UNPUBLISHED' and offers[0].get('sku')==LOT002_SKU)
        status='READY FOR CONTROLLED SETUP' if (all_ok and not offers) else ('EXISTING UNPUBLISHED LOT-002 FOUND — UPDATE IT' if existing_update else 'SAFETY STOP')
        button='''<form action="/ebay/lot002-setup" method="post">
          <p><label><input type="checkbox" name="shipping_verified" value="yes" required> I verified that <b>$%s USD</b> is the shipping amount I want charged to the eBay buyer for this parcel.</label></p>
          <button type="submit" style="font-size:18px;padding:14px 18px;font-weight:bold">CREATE LOT-002 INVENTORY + UNPUBLISHED OFFER</button>
        </form>''' % html.escape(str(draft.get('shipping_usd'))) if (all_ok and not offers) else ('''<form action="/ebay/lot002-fix-price" method="post"><input type="hidden" name="update_verified" value="yes"><p><b>Existing LOT-002 will be updated — no duplicate will be created.</b></p><button type="submit" style="font-size:18px;padding:14px 18px;font-weight:bold">UPDATE EXISTING LOT-002 + VERIFY</button></form>''' if existing_update else '')
        return page('LOT 002 eBay preflight',f'''<h2>LOT 002 — eBay Preflight</h2>
        <p><b>{status}</b></p>
        <p>SKU: <b>{LOT002_SKU}</b><br>Title: <b>{html.escape(draft.get('title') or LOT002_TITLE)}</b><br>Price: <b>${html.escape(str(draft.get('price') or LOT002_PRICE))}</b><br>Category: <b>{LOT002_CATEGORY_ID} — Collectible Telephones (Pre-1940)</b><br>Condition policy: <b>{html.escape(cond_detail)}</b><br>Photos: <b>{len(items)} selected (maximum 24)</b><br>Photo editor approved: <b>{'YES' if session.get('photo_editor_approved') else 'NO'}</b><br>MAIN PHOTO: <b>{('Photo ' + str(main_index)) if main_index else 'NOT SELECTED'}</b><br>Existing LOT-002 offer: <b>{html.escape(existing)}</b><br>Buyer shipping charge: <b>${html.escape(str(draft.get('shipping_usd')))}</b></p>
        <p><b>LOT 001 protection:</b> this pipeline uses a new SKU, new Offer, and a separate LOT002 fulfillment policy. It never updates policy 275629516012.</p>
        {button}''')
    except Exception as exc:
        return page('LOT 002 preflight error',f'<h2>Safety stop</h2><pre style="white-space:pre-wrap">{html.escape(str(exc))}</pre><p>Nothing was published.</p>'),500


@app.route('/ebay/lot002-setup',methods=['POST'])
def ebay_lot002_setup():
    draft=_lot002_current_draft()
    if not draft or request.form.get('shipping_verified')!='yes':
        return page('LOT 002 setup','<h2>Safety stop — draft or shipping confirmation missing.</h2>'),400
    try:
        token=get_ebay_access_token()
        used_ok,detail=_lot002_condition_used_allowed(token)
        if not used_ok:
            return page('LOT 002 setup',f'<h2>Safety stop — eBay category does not confirm USED.</h2><p>{html.escape(detail)}</p>'),409
        if _lot002_get_offers(token):
            return page('LOT 002 setup','<h2>Safety stop — LOT-002 already has an Offer.</h2><p>No duplicate was created.</p>'),409
        # Guard against an unexpected pre-existing inventory record. A 404 is the only clean state.
        inv_url=f'https://api.ebay.com/sell/inventory/v1/inventory_item/{LOT002_SKU}'
        chk=_lot002_request('GET',inv_url,headers=_lot002_headers(token),timeout=30)
        if chk.status_code == 200:
            return page('LOT 002 setup','<h2>Safety stop — LOT-002 Inventory Item already exists.</h2><p>It was not overwritten. Review it before retrying.</p>'),409
        if chk.status_code not in (404,):
            return page('LOT 002 setup',f'<h2>Could not verify clean LOT-002 SKU.</h2><p>HTTP {chk.status_code}</p><pre>{html.escape(chk.text[:4000])}</pre>'),502

        photos=_lot002_upload_edited_photos(token)
        if len(photos)!=24:
            raise RuntimeError('24 EPS URLs were not obtained.')
        policy_id=_lot002_get_or_create_shipping_policy(token,draft['shipping_usd'])

        description=draft.get('description') or draft.get('listing_text') or ''
        condition_note=(draft.get('condition_note') or
            'Vintage untested rotary telephone with substantial age and use wear. Heavy dirt/dust, scratches, rubbed surfaces, paint/coating loss and visible corrosion/rust are present. Rotary dial, handset and cords are present; operation and internal completeness are unknown. Sold as found for collection, display, restoration, repair or parts. Please review all 24 photos carefully.')
        aspects={
            'Brand':['Krasnaya Zarya'],
            'Type':['Rotary Dial Telephone'],
            'Color':['Black'],
            'Material':['Bakelite'],
        }
        inv_payload={
            'availability':{'shipToLocationAvailability':{'quantity':1}},
            'condition':'USED_EXCELLENT',
            'conditionDescription':condition_note[:1000],
            'product':{
                'title':(draft.get('title') or LOT002_TITLE)[:80],
                'description':description,
                'aspects':aspects,
                'imageUrls':[x['image_url'] for x in photos],
            },
        }
        ir=_lot002_request('PUT',inv_url,headers=_lot002_headers(token,True),json=inv_payload,timeout=60)
        if ir.status_code != 204:
            return page('LOT 002 inventory failed',f'<h2>Inventory Item creation failed</h2><p>HTTP {ir.status_code}</p><pre style="white-space:pre-wrap">{html.escape(ir.text[:8000])}</pre><p>No Offer was created and nothing was published.</p>'),502

        offer_payload={
            'sku':LOT002_SKU,'marketplaceId':LOT002_MARKETPLACE,'format':'FIXED_PRICE','availableQuantity':1,
            'listingDuration':'GTC',
            'categoryId':LOT002_CATEGORY_ID,'merchantLocationKey':LOT002_LOCATION_KEY,
            'listingDescription':description,
            'listingPolicies':{'paymentPolicyId':LOT002_PAYMENT_POLICY,'returnPolicyId':LOT002_RETURN_POLICY,'fulfillmentPolicyId':policy_id},
            'pricingSummary':{'price':{'value':draft.get('price') or LOT002_PRICE,'currency':'USD'}},
        }
        offer_url='https://api.ebay.com/sell/inventory/v1/offer'
        try:
            orr=requests.post(offer_url,headers=_lot002_headers(token,True),json=offer_payload,timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            # A reset may happen AFTER eBay accepted the POST. Never blindly
            # create a second Offer. Re-query first and recover the accepted one.
            time.sleep(2)
            recovered=_lot002_get_offers(token)
            if len(recovered)==1 and recovered[0].get('status')=='UNPUBLISHED' and recovered[0].get('sku')==LOT002_SKU:
                offer_id=str(recovered[0].get('offerId') or '')
                draft['offer_id']=offer_id; draft['fulfillment_policy_id']=policy_id; LOT002_DRAFT_CACHE[session.get('picker_session_id')]=draft
                return page('LOT 002 unpublished offer ready',f'''<h2>LOT 002 — UNPUBLISHED OFFER RECOVERED</h2><p>A temporary network reset occurred, but eBay had already created the Offer safely.</p><p>SKU: <b>{LOT002_SKU}</b><br>Offer ID: <b>{html.escape(offer_id)}</b><br>Photos: <b>24/24</b><br>Shipping: <b>${html.escape(str(draft['shipping_usd']))}</b></p><p><b>Nothing is live yet.</b></p><p><a href="/ebay/publish-lot002" style="font-size:18px">Run final eBay verification →</a></p>''')
            raise
        if orr.status_code not in (200,201):
            return page('LOT 002 offer failed',f'<h2>Inventory exists, but Offer creation failed</h2><p>HTTP {orr.status_code}</p><pre style="white-space:pre-wrap">{html.escape(orr.text[:8000])}</pre><p>Nothing was published.</p>'),502
        od=orr.json() if orr.text.strip() else {}; offer_id=str(od.get('offerId') or '')
        if not offer_id:
            return page('LOT 002 offer failed','<h2>Offer response did not contain offerId.</h2><p>Nothing was published.</p>'),502
        draft['offer_id']=offer_id; draft['fulfillment_policy_id']=policy_id; LOT002_DRAFT_CACHE[session.get('picker_session_id')]=draft
        return page('LOT 002 unpublished offer ready',f'''<h2>LOT 002 — UNPUBLISHED OFFER CREATED</h2><p>SKU: <b>{LOT002_SKU}</b><br>Offer ID: <b>{html.escape(offer_id)}</b><br>Photos: <b>24/24</b><br>Separate fulfillment policy: <b>{html.escape(policy_id)}</b><br>Shipping: <b>${html.escape(str(draft['shipping_usd']))}</b></p><p><b>Nothing is live yet.</b></p><p><a href="/ebay/publish-lot002" style="font-size:18px">Run final eBay verification →</a></p>''')
    except Exception as exc:
        return page('LOT 002 setup error',f'<h2>LOT 002 setup stopped</h2><pre style="white-space:pre-wrap">{html.escape(str(exc))}</pre><p>publishOffer was not called.</p>'),500


@app.route('/ebay/lot002-fix-price', methods=['GET','POST'])
def ebay_lot002_fix_price():
    target_price = LOT002_PRICE
    try:
        token = get_ebay_access_token(); offers = _lot002_get_offers(token)
        if len(offers) != 1:
            return page('LOT 002 price safety stop', f'<h2>Safety stop — expected exactly one LOT-002 Offer, found {len(offers)}.</h2><p>Nothing was changed.</p>'), 409
        offer = offers[0]; offer_id = str(offer.get('offerId') or '')
        if offer.get('status') != 'UNPUBLISHED' or offer.get('sku') != LOT002_SKU:
            return page('LOT 002 price safety stop','<h2>Safety stop — LOT-002 is not the expected UNPUBLISHED offer.</h2><p>Nothing was changed.</p>'),409
        current_price = str(((offer.get('pricingSummary') or {}).get('price') or {}).get('value') or '')
        if request.method == 'GET':
            return page('LOT 002 price correction', f"""<h2>LOT 002 — PRICE CORRECTION</h2><p>Offer ID: <b>{html.escape(offer_id)}</b><br>Current eBay price: <b>${html.escape(current_price)}</b><br>New price: <b>${html.escape(target_price)}</b></p><p><b>This changes only the existing UNPUBLISHED LOT-002 offer. It does not publish it and does not touch LOT-001.</b></p><form method=\"post\"><button type=\"submit\" style=\"font-size:18px;padding:14px 18px;font-weight:bold\">CHANGE LOT-002 PRICE TO ${html.escape(target_price)}</button></form>""")
        if request.form.get('update_verified') not in (None, 'yes'):
            return page('LOT 002 update safety stop','<h2>Safety stop — update confirmation missing.</h2><p>Nothing was changed.</p>'),400
        draft=_lot002_current_draft() or {}
        target_shipping=str(draft.get('shipping_usd') or '')
        if not target_shipping:
            return page('LOT 002 update safety stop','<h2>Safety stop — current shipping amount is missing.</h2><p>Nothing was changed.</p>'),409
        policy_id=_lot002_get_or_create_shipping_policy(token,target_shipping)
        policies=dict(offer.get('listingPolicies') or {})
        policies['fulfillmentPolicyId']=policy_id
        payload = {'sku':offer.get('sku'),'marketplaceId':offer.get('marketplaceId'),'format':offer.get('format'),'availableQuantity':offer.get('availableQuantity'),'categoryId':offer.get('categoryId'),'merchantLocationKey':offer.get('merchantLocationKey'),'listingDuration':offer.get('listingDuration') or 'GTC','listingPolicies':policies,'pricingSummary':{'price':{'value':target_price,'currency':'USD'}}}
        for key in ('listingDescription','quantityLimitPerBuyer','storeCategoryNames','tax','charity','lotSize','includeCatalogProductDetails','listingStartDate'):
            if key in offer and offer.get(key) is not None: payload[key]=offer.get(key)
        r=requests.put(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=_lot002_headers(token,True),json=payload,timeout=45)
        if r.status_code not in (200,204):
            return page('LOT 002 price update failed',f'<h2>eBay did not change the price</h2><p>HTTP {r.status_code}</p><pre style=\"white-space:pre-wrap\">{html.escape(r.text[:10000])}</pre><p>Nothing was published.</p>'),502
        fresh=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=_lot002_headers(token),timeout=30)
        if fresh.status_code!=200: return page('LOT 002 price verify failed','<h2>Could not verify the updated offer. Do not publish yet.</h2>'),502
        fj=fresh.json() or {}; fresh_price=str(((fj.get('pricingSummary') or {}).get('price') or {}).get('value') or '')
        fresh_policy=str((fj.get('listingPolicies') or {}).get('fulfillmentPolicyId') or '')
        if fj.get('status')!='UNPUBLISHED' or fj.get('sku')!=LOT002_SKU or fresh_price!=target_price or fresh_policy!=policy_id:
            return page('LOT 002 price verify failed',f'<h2>Final price verification failed.</h2><p>Status: {html.escape(str(fj.get("status")))}<br>SKU: {html.escape(str(fj.get("sku")))}<br>Price: ${html.escape(fresh_price)}</p><p>Do not publish yet.</p>'),409
        draft=_lot002_current_draft(); draft['price']=target_price
        if session.get('picker_session_id'): LOT002_DRAFT_CACHE[session.get('picker_session_id')]=draft
        return redirect('/ebay/publish-lot002')
    except Exception as exc:
        return page('LOT 002 price correction error',f'<h2>LOT 002 price correction error</h2><pre>{html.escape(str(exc))}</pre><p>Nothing was published.</p>'),500


@app.route('/ebay/publish-lot002',methods=['GET','POST'])
def ebay_publish_lot002():
    draft=_lot002_current_draft()
    try:
        token=get_ebay_access_token(); offers=_lot002_get_offers(token)
        if len(offers)!=1:
            return page('LOT 002 publish stop',f'<h2>Safety stop — expected exactly one LOT-002 Offer, found {len(offers)}.</h2><p>publishOffer was not called.</p>'),409
        offer=offers[0]; offer_id=str(offer.get('offerId') or '')
        invr=requests.get(f'https://api.ebay.com/sell/inventory/v1/inventory_item/{LOT002_SKU}',headers=_lot002_headers(token),timeout=30)
        if invr.status_code!=200:
            return page('LOT 002 publish stop',f'<h2>Inventory read failed: HTTP {invr.status_code}</h2><p>publishOffer was not called.</p>'),502
        inv=invr.json(); product=inv.get('product') or {}; images=product.get('imageUrls') or []; lp=offer.get('listingPolicies') or {}; price=(offer.get('pricingSummary') or {}).get('price') or {}
        policy_id=str(lp.get('fulfillmentPolicyId') or '')
        # Read the separate policy and verify its shipping cost.
        pr=requests.get(f'https://api.ebay.com/sell/account/v1/fulfillment_policy/{policy_id}',headers=_lot002_headers(token),timeout=30)
        policy_cost=''; service=''
        if pr.status_code==200:
            for opt in pr.json().get('shippingOptions') or []:
                for svc in opt.get('shippingServices') or []:
                    if svc.get('shippingServiceCode')=='StandardShippingFromOutsideUS':
                        service=svc.get('shippingServiceCode'); policy_cost=str((svc.get('shippingCost') or {}).get('value') or '')
        expected_ship=str(draft.get('shipping_usd') or '')
        try: ship_ok=abs(float(policy_cost)-float(expected_ship))<0.001
        except Exception: ship_ok=False
        checks=[
            ('Offer status',offer.get('status'),'UNPUBLISHED',offer.get('status')=='UNPUBLISHED'),
            ('SKU',offer.get('sku'),LOT002_SKU,offer.get('sku')==LOT002_SKU),
            ('Marketplace',offer.get('marketplaceId'),LOT002_MARKETPLACE,offer.get('marketplaceId')==LOT002_MARKETPLACE),
            ('Category',str(offer.get('categoryId') or ''),LOT002_CATEGORY_ID,str(offer.get('categoryId') or '')==LOT002_CATEGORY_ID),
            ('Listing duration',offer.get('listingDuration'),'GTC',offer.get('listingDuration')=='GTC'),
            ('Quantity',offer.get('availableQuantity'),1,offer.get('availableQuantity')==1),
            ('Price',price.get('value'),draft.get('price') or LOT002_PRICE,str(price.get('value'))==str(draft.get('price') or LOT002_PRICE)),
            ('Currency',price.get('currency'),'USD',price.get('currency')=='USD'),
            ('Location',offer.get('merchantLocationKey'),LOT002_LOCATION_KEY,offer.get('merchantLocationKey')==LOT002_LOCATION_KEY),
            ('Payment policy',lp.get('paymentPolicyId'),LOT002_PAYMENT_POLICY,lp.get('paymentPolicyId')==LOT002_PAYMENT_POLICY),
            ('Return policy',lp.get('returnPolicyId'),LOT002_RETURN_POLICY,lp.get('returnPolicyId')==LOT002_RETURN_POLICY),
            ('Separate fulfillment policy',policy_id,'NOT LOT001',bool(policy_id) and policy_id!='275629516012'),
            ('Shipping service',service,'StandardShippingFromOutsideUS',service=='StandardShippingFromOutsideUS'),
            ('Shipping cost',policy_cost,expected_ship,ship_ok),
            ('Condition',inv.get('condition'),'USED_EXCELLENT',inv.get('condition')=='USED_EXCELLENT'),
            ('EPS photos',len(images),f'1–24 (expected {len(exp_urls)})',1<=len(images)<=24 and len(images)==len(exp_urls)),
            ('Title',product.get('title'),draft.get('title') or LOT002_TITLE,product.get('title')==(draft.get('title') or LOT002_TITLE)),
        ]
        all_ok=all(x[3] for x in checks)
        rows=''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(('PASS' if ok else 'FAIL'),html.escape(str(n)),html.escape(str(a)),html.escape(str(e))) for n,a,e,ok in checks)
        if not all_ok:
            # One-flow repair: if the ONLY mismatch is the Inventory Item title,
            # repair that title in-place and immediately run this authoritative
            # verification again. This never publishes and never touches LOT-001.
            failed=[x for x in checks if not x[3]]
            if request.method=='GET' and len(failed)==1 and failed[0][0]=='Title':
                expected_title=str(draft.get('title') or LOT002_TITLE)
                inv_payload={}
                for key in ('availability','condition','conditionDescription','packageWeightAndSize','product'):
                    if key in inv and inv.get(key) is not None:
                        inv_payload[key]=inv.get(key)
                inv_payload['product']=dict(inv_payload.get('product') or {})
                inv_payload['product']['title']=expected_title
                fix=requests.put(
                    f'https://api.ebay.com/sell/inventory/v1/inventory_item/{LOT002_SKU}',
                    headers=_lot002_headers(token,True),json=inv_payload,timeout=60)
                if fix.status_code not in (200,204):
                    return page('LOT 002 title repair failed',f'<h2>LOT 002 — TITLE UPDATE FAILED</h2><p>HTTP {fix.status_code}</p><pre style="white-space:pre-wrap">{html.escape(fix.text[:10000])}</pre><p><b>Nothing was published.</b></p>'),502
                return redirect('/ebay/publish-lot002')
            return page('LOT 002 not ready',f'<h2>LOT 002 — NOT READY TO PUBLISH</h2><table border="1" cellpadding="5"><tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}</table><p><b>publishOffer was not called.</b></p>'),409
        if request.method=='GET':
            return page('LOT 002 final confirmation',f'''<h2>LOT 002 — FINAL PUBLISH CONFIRMATION</h2><p><b>All authoritative eBay checks passed.</b></p><table border="1" cellpadding="5"><tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}</table><h3>The next button WILL publish LOT 002 live on eBay.com.</h3><form method="post"><button type="submit" style="font-size:18px;padding:14px 18px;font-weight:bold">PUBLISH LOT-002 LIVE ON EBAY</button></form>''')
        # Re-read offer immediately before the consequential call.
        fresh=requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',headers=_lot002_headers(token),timeout=30)
        if fresh.status_code!=200 or (fresh.json() or {}).get('status')!='UNPUBLISHED' or (fresh.json() or {}).get('sku')!=LOT002_SKU:
            return page('LOT 002 publish stop','<h2>Final re-check failed. publishOffer was not called.</h2>'),409
        pub=requests.post(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',headers=_lot002_headers(token,True),timeout=45)
        if pub.status_code not in (200,201):
            return page('LOT 002 publish failed',f'<h2>eBay did not publish LOT 002</h2><p>HTTP {pub.status_code}</p><pre style="white-space:pre-wrap">{html.escape(pub.text[:10000])}</pre>'),502
        pd=pub.json() if pub.text.strip() else {}; listing_id=str(pd.get('listingId') or '')
        return page('LOT 002 PUBLISHED',f'''<h2>LOT 002 — PUBLISHED ON EBAY</h2><p>Listing ID: <b>{html.escape(listing_id)}</b><br>Offer ID: <b>{html.escape(offer_id)}</b><br>SKU: <b>{LOT002_SKU}</b></p><p><b>Do not press Publish again.</b></p>''')
    except Exception as exc:
        return page('LOT 002 publish error',f'<h2>LOT 002 publish error</h2><pre style="white-space:pre-wrap">{html.escape(str(exc))}</pre><p>Do not retry until reviewed.</p>'),500


# =========================================================
# GENERIC CURRENT-LOT PIPELINE — LOT 003 AND FOLLOWING LOTS
# =========================================================

LOT_COUNTER_SCHEMA_VERSION = "production-v4.1"

def _discover_next_lot_from_ebay():
    """Recover the next LOT number after a Render redeploy from published eBay offers."""
    highest = 0
    try:
        token = get_ebay_access_token()
        headers = _current_headers(token)
        r = _lot002_request('GET','https://api.ebay.com/sell/inventory/v1/offer',headers=headers,params={'limit':200},timeout=30)
        if r.status_code == 200:
            for offer in (r.json() or {}).get('offers') or []:
                if str(offer.get('status') or '').upper() != 'PUBLISHED':
                    continue
                sku = str(offer.get('sku') or '')
                m = re.fullmatch(r'LOT-(\d{3,})', sku)
                if m:
                    highest = max(highest, int(m.group(1)))
        if highest == 0:
            r = _lot002_request('GET','https://api.ebay.com/sell/inventory/v1/inventory_item',headers=headers,params={'limit':200},timeout=30)
            if r.status_code == 200:
                for item in (r.json() or {}).get('inventoryItems') or []:
                    sku = str(item.get('sku') or '')
                    m = re.fullmatch(r'LOT-(\d{3,})', sku)
                    if m:
                        highest = max(highest, int(m.group(1)))
    except Exception:
        pass
    return max(4, highest + 1 if highest else 4)

def _sync_current_lot_once():
    """Keep the browser session aligned with eBay after deploy/sleep/restart.

    The Flask session cookie can survive a Render process restart while all
    in-memory caches disappear. Therefore the schema flag alone is not enough:
    at each new-lot entry we reconcile the session counter with eBay and only
    move it FORWARD, never backward.
    """
    try:
        discovered = int(_discover_next_lot_from_ebay())
    except Exception:
        discovered = 4
    try:
        current = int(session.get('current_lot_number') or 4)
    except Exception:
        current = 4
    session['current_lot_number'] = max(4, current, discovered)
    session['lot_counter_schema'] = LOT_COUNTER_SCHEMA_VERSION
    return int(session['current_lot_number'])

def _current_lot_number():
    try:
        return max(4, int(session.get('current_lot_number') or _sync_current_lot_once()))
    except Exception:
        return 4


def _current_lot_label():
    return f'LOT {_current_lot_number():03d}'


def _current_lot_sku():
    return f'LOT-{_current_lot_number():03d}'


def _natural_key(text):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', str(text or ''))]


def _photo_capture_value(item, fallback_index):
    mf=item.get('mediaFile') or {}
    md=mf.get('mediaFileMetadata') or mf.get('mediaMetadata') or {}
    for obj in (md,mf,item):
        if not isinstance(obj,dict):
            continue
        for key in ('creationTime','createTime','mediaCreationTime','timestamp'):
            val=obj.get(key)
            if val:
                return (0,str(val),fallback_index)
    name=str(mf.get('filename') or '')
    if name:
        return (1,_natural_key(name),fallback_index)
    return (2,fallback_index)


def _build_logical_photo_order(items, main_index):
    """Seller-authoritative photo order.

    Google Picker order is the order shown to the seller in our editor. After
    MAIN PHOTO is chosen, only that photo is moved to position 1. Every other
    photo keeps its exact visible Picker/editor order. Do NOT sort by capture
    time or filename after seller approval.
    """
    indexed = list(range(1, len(items) + 1))
    if main_index not in indexed:
        return indexed
    return [main_index] + [i for i in indexed if i != main_index]


def _current_draft():
    return LOT002_DRAFT_CACHE.get(session.get('picker_session_id')) or {}


def _current_headers(token,json_body=False):
    return _lot002_headers(token,json_body)


def _current_get_offers(token):
    r=_lot002_request('GET','https://api.ebay.com/sell/inventory/v1/offer',headers=_current_headers(token),params={'sku':_current_lot_sku(),'marketplace_id':'EBAY_US'},timeout=30)
    if r.status_code==404:
        try:
            ids={str(e.get('errorId')) for e in (r.json().get('errors') or [])}
        except Exception:
            ids=set()
        if '25713' in ids:
            return []
    if r.status_code!=200:
        raise RuntimeError(f'Could not check offers: HTTP {r.status_code}: {r.text[:2500]}')
    return r.json().get('offers') or []


def _current_category(token,title):
    tr=_lot002_request('GET','https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id',headers=_current_headers(token),params={'marketplace_id':'EBAY_US'},timeout=30)
    if tr.status_code!=200:
        raise RuntimeError(f'Taxonomy tree failed: HTTP {tr.status_code}')
    tree=str((tr.json() or {}).get('categoryTreeId') or '')
    sr=_lot002_request('GET',f'https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree}/get_category_suggestions',headers=_current_headers(token),params={'q':title[:120]},timeout=30)
    if sr.status_code!=200:
        raise RuntimeError(f'Category suggestions failed: HTTP {sr.status_code}: {sr.text[:2500]}')
    sugg=(sr.json() or {}).get('categorySuggestions') or []
    if not sugg:
        raise RuntimeError('eBay returned no category suggestion for this title.')
    cat=sugg[0].get('category') or {}
    return str(cat.get('categoryId') or ''), str(cat.get('categoryName') or '')


_COND_ENUM={'3000':'USED_EXCELLENT','4000':'USED_VERY_GOOD','5000':'USED_GOOD','6000':'USED_ACCEPTABLE','7000':'FOR_PARTS_OR_NOT_WORKING'}


def _current_condition(token,category_id,draft):
    r=_lot002_request('GET','https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_condition_policies',headers=_current_headers(token),params={'category_ids':category_id},timeout=30)
    if r.status_code!=200:
        raise RuntimeError(f'Condition policy failed: HTTP {r.status_code}: {r.text[:2500]}')
    allowed=[]
    for pol in (r.json() or {}).get('itemConditionPolicies') or []:
        if str(pol.get('categoryId') or '')==str(category_id):
            allowed=[str(x.get('conditionId') or '') for x in (pol.get('itemConditions') or [])]
            break
    text=((draft.get('condition_note') or '')+' '+(draft.get('listing_text') or '')+' '+str(draft.get('seller_tested_status') or '')).casefold()
    broken=any(x in text for x in ('not working','non-working','for parts','parts or repair','repair or parts','tested not working'))
    severe=any(x in text for x in ('heavy wear','severe wear','crack','broken','major damage','significant damage','heavy rust','corrosion','missing parts'))
    normal_wear=any(x in text for x in ('wear','scratch','scuff','chip','age-related','used condition','surface wear'))
    excellent=any(x in text for x in ('excellent condition','near mint','like new'))
    very_good=any(x in text for x in ('very good condition','very good overall'))
    if broken:
        pref=['7000','6000','5000','4000','3000']
    elif severe:
        pref=['6000','5000','4000','3000','7000']
    elif excellent:
        pref=['3000','4000','5000','6000','7000']
    elif very_good:
        pref=['4000','5000','3000','6000','7000']
    elif normal_wear:
        pref=['5000','4000','6000','3000','7000']
    else:
        pref=['5000','4000','3000','6000','7000']
    cid=next((x for x in pref if x in allowed and x in _COND_ENUM),None)
    if not cid:
        raise RuntimeError(f'No supported used condition in category {category_id}. eBay returned {allowed}.')
    return cid,_COND_ENUM[cid]


def _current_aspects(draft):
    out={}
    raw=draft.get('item_specifics') if isinstance(draft.get('item_specifics'),dict) else {}
    if raw:
        for k,v in raw.items():
            k=str(k).strip(); vals=v if isinstance(v,list) else [v]
            vals=[str(x).strip() for x in vals if str(x).strip()]
            vals=[x for x in vals if not any(t in x.casefold() for t in ('unknown','not verified','uncertain','not applicable','does not apply','n/a'))]
            if k and len(k)<=65 and vals:
                out[k]=vals[:5]
    else:
        block=_lot002_extract_section(draft.get('listing_text') or '',4,5)
        for line in block.splitlines():
            if ':' not in line: continue
            k,v=line.split(':',1); k=k.strip(); v=v.strip(); low=v.casefold()
            if not k or not v or len(k)>65 or len(v)>65: continue
            if any(x in low for x in ('unknown','not verified','uncertain','not applicable','does not apply','n/a')): continue
            out[k]=[v]
    material=str(draft.get('confirmed_material') or '').strip()
    if material:
        vals=[x.strip() for x in re.split(r'[,;/]',material) if x.strip()]
        if vals: out['Material']=vals
    else:
        out.pop('Material',None)
    return out


def _current_best_offer_supported(token, category_id):
    """Return True only when eBay metadata says this leaf category supports negotiated Best Offer."""
    r=_lot002_request(
        'GET',
        'https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_negotiated_price_policies',
        headers=_current_headers(token),
        params={'filter': f'categoryIds:{{{category_id}}}'},
        timeout=30,
    )
    if r.status_code!=200:
        return False
    for pol in (r.json() or {}).get('negotiatedPricePolicies') or []:
        if str(pol.get('categoryId') or '')==str(category_id):
            return True
    return False


def _current_shipping_policy(token,shipping_usd):
    cost=f'{float(shipping_usd):.2f}'
    name=f'Vintage UA eBay Shipping {_current_lot_sku()} {cost}'
    r=_lot002_request('GET','https://api.ebay.com/sell/account/v1/fulfillment_policy',headers=_current_headers(token),params={'marketplace_id':'EBAY_US'},timeout=30)
    if r.status_code!=200:
        raise RuntimeError(f'Fulfillment policy read failed: HTTP {r.status_code}')
    for p in (r.json() or {}).get('fulfillmentPolicies') or []:
        if str(p.get('name') or '').strip().casefold()==name.casefold():
            return str(p.get('fulfillmentPolicyId') or '')
    payload={'name':name,'marketplaceId':'EBAY_US','categoryTypes':[{'name':'ALL_EXCLUDING_MOTORS_VEHICLES'}],'handlingTime':{'value':3,'unit':'DAY'},'shippingOptions':[{'optionType':'DOMESTIC','costType':'FLAT_RATE','shippingServices':[{'sortOrder':1,'shippingServiceCode':'StandardShippingFromOutsideUS','shippingCost':{'value':cost,'currency':'USD'},'additionalShippingCost':{'value':'0.00','currency':'USD'},'freeShipping':False}]}],'globalShipping':False,'pickupDropOff':False,'freightShipping':False}
    c=requests.post('https://api.ebay.com/sell/account/v1/fulfillment_policy',headers=_current_headers(token,True),json=payload,timeout=45)
    if c.status_code not in (200,201):
        raise RuntimeError(f'Fulfillment policy creation failed: HTTP {c.status_code}: {c.text[:3000]}')
    pid=str((c.json() if c.text.strip() else {}).get('fulfillmentPolicyId') or '')
    if not pid:
        raise RuntimeError('No fulfillmentPolicyId returned.')
    return pid


def _current_upload_photos(token):
    picker_id=session.get('picker_session_id'); items=PHOTO_CACHE.get(picker_id,[])
    if not (1 <= len(items) <= 24) or not session.get('photo_editor_approved'):
        raise RuntimeError('From 1 to 24 approved photos are required.')
    sku=_current_lot_sku(); cached=EBAY_MEDIA_CACHE.get(sku) or []
    if len(cached)==len(items) and len(items)>=1 and all(x.get('image_url') for x in cached):
        return cached
    main=int(session.get('main_photo_index') or 0)
    order=session.get('photo_order') or _build_logical_photo_order(items,main)
    order=[int(x) for x in order]
    if len(order)!=len(items) or sorted(order)!=list(range(1,len(items)+1)) or not order or order[0]!=main:
        order=_build_logical_photo_order(items,main)
    results=[]
    for pos,i in enumerate(order,1):
        mf=(items[i-1].get('mediaFile') or {}); filename=mf.get('filename') or f'{sku}-{i:02d}.jpg'
        data=_download_picker_photo(i,edited=True,max_side=2400)
        mr=requests.post('https://apim.ebay.com/commerce/media/v1_beta/image/create_image_from_file',headers={'Authorization':f'Bearer {token}','Accept':'application/json'},files={'image':(filename,data,'image/jpeg')},timeout=120)
        if mr.status_code!=201:
            raise RuntimeError(f'EPS upload failed at source photo {i}: HTTP {mr.status_code}: {mr.text[:2500]}')
        jd=mr.json() if mr.text.strip() else {}; loc=mr.headers.get('Location',''); iid=loc.rstrip('/').split('/')[-1] if loc else ''
        url=jd.get('maxDimensionImageUrl') or jd.get('imageUrl') or ''
        if iid:
            for attempt in range(5):
                gr=_lot002_request('GET',f'https://apim.ebay.com/commerce/media/v1_beta/image/{iid}',headers={'Authorization':f'Bearer {token}','Accept':'application/json'},timeout=30)
                if gr.status_code==200:
                    gd=gr.json() or {}; url=gd.get('maxDimensionImageUrl') or gd.get('imageUrl') or url
                    if url:
                        break
                time.sleep(1.5*(attempt+1))
        if not url:
            raise RuntimeError(f'EPS photo {i} has no usable URL after verification.')
        results.append({'number':pos,'source_number':i,'image_id':iid,'image_url':url,'filename':filename,'is_main':pos==1})
    EBAY_MEDIA_CACHE[sku]=results; session['photo_order']=order
    return results


@app.route('/ebay/current-preflight')
def ebay_current_preflight():
    draft=_current_draft(); label=_current_lot_label(); sku=_current_lot_sku(); items=PHOTO_CACHE.get(session.get('picker_session_id'),[])
    if not draft:
        return page(label+' preflight','<h2>Draft is missing from current Render memory.</h2>'),400
    try:
        title=str(draft.get('title') or '').strip(); price=str(draft.get('price') or '').strip(); ship=str(draft.get('shipping_usd') or '').strip()
        confirmed_material=str(draft.get('confirmed_material') or '').strip()
        full_text=(title+' '+str(draft.get('description') or '')+' '+str(draft.get('listing_text') or '')).casefold()
        cm=confirmed_material.casefold()
        conflicts=[]
        known_material_words={'wood':['wood','wooden'],'plastic':['plastic'],'metal':['metal'],'glass':['glass'],'ceramic':['ceramic'],'leather':['leather'],'rubber':['rubber']}
        for canonical, words in known_material_words.items():
            if canonical not in cm and any(re.search(r'\b'+re.escape(w)+r'\b', full_text) for w in words):
                conflicts.append(canonical)
        if confirmed_material and conflicts:
            raise RuntimeError('Seller-fact conflict: listing mentions unconfirmed material(s): '+', '.join(conflicts)+'. Go back and regenerate the final draft.')
        if not title or not price or not ship:
            raise RuntimeError('Draft title, price or shipping is missing. No fallback values will be invented.')
        token=get_ebay_access_token(); cat_id,cat_name=_current_category(token,title); cond_id,cond_enum=_current_condition(token,cat_id,draft)
        best_offer_supported=_current_best_offer_supported(token,cat_id); draft.update({'category_id':cat_id,'category_name':cat_name,'condition_id':cond_id,'condition_enum':cond_enum,'best_offer_supported':best_offer_supported}); LOT002_DRAFT_CACHE[session.get('picker_session_id')]=draft
        offers=_current_get_offers(token); main=int(session.get('main_photo_index') or 0); order=session.get('photo_order') or []
        all_ok=(1<=len(items)<=24 and bool(session.get('photo_editor_approved')) and 1<=main<=len(items) and len(order)==len(items) and bool(order) and order[0]==main and not offers and bool(draft.get('price_confirmed')) and bool(draft.get('seller_reviewed')))
        order_text=', '.join(str(x) for x in order)
        button=''
        if all_ok:
            button=f'<form action="/ebay/current-setup" method="post"><label><input type="checkbox" name="shipping_verified" value="yes" required> I verified shipping ${html.escape(ship)}.</label><br><br><button type="submit" style="font-size:18px;padding:14px 18px;font-weight:bold">CREATE {label} INVENTORY + UNPUBLISHED OFFER</button></form>'
        return page(label+' preflight',f'<h2>{label} — eBay Preflight</h2><p><b>{"READY" if all_ok else "SAFETY STOP"}</b></p><p>SKU: <b>{sku}</b><br>Title: <b>{html.escape(title)}</b><br>Price: <b>${html.escape(price)} (SELLER CONFIRMED)</b><br>Material(s): <b>{html.escape(str(draft.get('confirmed_material') or 'Unknown / omitted'))} (reviewed)</b><br>Category: <b>{html.escape(cat_id)} — {html.escape(cat_name)}</b><br>Condition: <b>{html.escape(cond_enum)} / {html.escape(cond_id)}</b><br>Photos: <b>{len(items)} selected (maximum 24)</b><br>Main photo: <b>source photo {main}</b><br>Photo order: <b>{html.escape(order_text)}</b><br>Shipping: <b>${html.escape(ship)}</b><br>Best Offer: <b>{'ENABLED' if draft.get('best_offer_supported') else 'not supported by this eBay category'}</b></p><p>Photo order is seller-authoritative: MAIN PHOTO first, then the remaining photos exactly in the order shown in the editor.</p>{button}')
    except Exception as exc:
        return page(label+' preflight error',f'<h2>Safety stop</h2><pre>{html.escape(str(exc))}</pre><p>Nothing was published.</p>'),500


@app.route('/ebay/current-setup',methods=['POST'])
def ebay_current_setup():
    draft=_current_draft(); label=_current_lot_label(); sku=_current_lot_sku()
    if not draft or request.form.get('shipping_verified')!='yes' or not draft.get('price_confirmed') or not draft.get('seller_reviewed'):
        return page(label+' setup','<h2>Safety stop — draft, shipping, reviewed price, or seller review is missing.</h2><p>Nothing was sent to eBay.</p>'),400
    try:
        token=get_ebay_access_token()
        if _current_get_offers(token):
            return page(label+' setup',f'<h2>{sku} already has an Offer. No duplicate was created.</h2>'),409
        inv_url=f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
        chk=_lot002_request('GET',inv_url,headers=_current_headers(token),timeout=30)
        if chk.status_code==200:
            return page(label+' setup',f'<h2>{sku} inventory already exists. It was not overwritten.</h2>'),409
        if chk.status_code!=404:
            raise RuntimeError(f'Could not confirm clean SKU: HTTP {chk.status_code}: {chk.text[:2000]}')
        photos=_current_upload_photos(token); pid=_current_shipping_policy(token,draft['shipping_usd']); desc=draft.get('description') or draft.get('listing_text') or ''
        product={'title':str(draft['title'])[:80],'description':desc,'imageUrls':[x['image_url'] for x in photos]}; asp=_current_aspects(draft)
        if asp: product['aspects']=asp
        inv={'availability':{'shipToLocationAvailability':{'quantity':1}},'condition':draft['condition_enum'],'conditionDescription':(draft.get('condition_note') or '')[:1000],'product':product}
        ir=_lot002_request('PUT',inv_url,headers=_current_headers(token,True),json=inv,timeout=60)
        if ir.status_code!=204:
            return page(label+' inventory failed',f'<h2>Inventory creation failed</h2><p>HTTP {ir.status_code}</p><pre>{html.escape(ir.text[:7000])}</pre>'),502
        offer={'sku':sku,'marketplaceId':'EBAY_US','format':'FIXED_PRICE','availableQuantity':1,'listingDuration':'GTC','categoryId':draft['category_id'],'merchantLocationKey':LOT002_LOCATION_KEY,'listingDescription':desc,'listingPolicies':{'paymentPolicyId':LOT002_PAYMENT_POLICY,'returnPolicyId':LOT002_RETURN_POLICY,'fulfillmentPolicyId':pid, **({'bestOfferTerms':{'bestOfferEnabled':True}} if draft.get('best_offer_supported') else {})},'pricingSummary':{'price':{'value':str(draft['price']),'currency':'USD'}}}
        try:
            orr=requests.post('https://api.ebay.com/sell/inventory/v1/offer',headers=_current_headers(token,True),json=offer,timeout=60)
        except (requests.exceptions.ConnectionError,requests.exceptions.Timeout):
            time.sleep(2); rec=_current_get_offers(token)
            if len(rec)==1 and rec[0].get('status')=='UNPUBLISHED':
                return redirect('/ebay/current-publish')
            raise
        if orr.status_code not in (200,201):
            return page(label+' offer failed',f'<h2>Offer creation failed</h2><p>HTTP {orr.status_code}</p><pre>{html.escape(orr.text[:7000])}</pre>'),502
        return redirect('/ebay/current-publish')
    except Exception as exc:
        return page(label+' setup error',f'<h2>{label} setup stopped</h2><pre>{html.escape(str(exc))}</pre><p>publishOffer was not called.</p>'),500


@app.route('/ebay/current-publish',methods=['GET','POST'])
def ebay_current_publish():
    draft=_current_draft(); label=_current_lot_label(); sku=_current_lot_sku()
    try:
        token=get_ebay_access_token(); offers=_current_get_offers(token)
        if len(offers)!=1:
            # A published offer may disappear from the SKU-filtered getOffers
            # result. This is normal after successful publication or after a
            # Render restart. Reconcile with eBay instead of showing a false
            # fatal error or trying to publish again.
            if len(offers) == 0:
                discovered = _discover_next_lot_from_ebay()
                try:
                    current_no = int(re.search(r'(\d+)$', sku).group(1))
                except Exception:
                    current_no = _current_lot_number()
                if discovered > current_no:
                    session['current_lot_number'] = discovered
                    session['lot_counter_schema'] = LOT_COUNTER_SCHEMA_VERSION
                    return page(
                        label+' already published',
                        f'<h2>{label} is already published on eBay.</h2>'
                        f'<p>The live eBay state was recovered after restart. Nothing was published again.</p>'
                        f'<form action="/next-lot" method="post"><button type="submit" style="font-size:20px;padding:15px 20px;font-weight:bold">START LOT {discovered:03d}</button></form>'
                    )
            return page(label+' publish stop',f'<h2>Expected one unpublished Offer, found {len(offers)}.</h2><p>Nothing was published.</p>'),409
        offer=offers[0]; oid=str(offer.get('offerId') or '')
        invr=_lot002_request('GET',f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}',headers=_current_headers(token),timeout=30)
        if invr.status_code!=200: raise RuntimeError(f'Inventory read failed HTTP {invr.status_code}')
        inv=invr.json() or {}; product=inv.get('product') or {}; images=product.get('imageUrls') or []; lp=offer.get('listingPolicies') or {}; price=(offer.get('pricingSummary') or {}).get('price') or {}
        pid=str(lp.get('fulfillmentPolicyId') or ''); pr=_lot002_request('GET',f'https://api.ebay.com/sell/account/v1/fulfillment_policy/{pid}',headers=_current_headers(token),timeout=30)
        cost=''; service=''
        if pr.status_code==200:
            for opt in (pr.json() or {}).get('shippingOptions') or []:
                for svc in opt.get('shippingServices') or []:
                    if svc.get('shippingServiceCode')=='StandardShippingFromOutsideUS': service='StandardShippingFromOutsideUS'; cost=str((svc.get('shippingCost') or {}).get('value') or '')
        exp_ship=str(draft.get('shipping_usd') or '')
        try: ship_ok=abs(float(cost)-float(exp_ship))<0.001
        except Exception: ship_ok=False
        cached=EBAY_MEDIA_CACHE.get(sku) or []; exp_urls=[x.get('image_url') for x in cached]; photo_order_ok=(1<=len(images)<=24 and len(images)==len(exp_urls) and images==exp_urls)
        checks=[('Offer status',offer.get('status'),'UNPUBLISHED',offer.get('status')=='UNPUBLISHED'),('SKU',offer.get('sku'),sku,offer.get('sku')==sku),('Marketplace',offer.get('marketplaceId'),'EBAY_US',offer.get('marketplaceId')=='EBAY_US'),('Category',str(offer.get('categoryId') or ''),str(draft.get('category_id') or ''),str(offer.get('categoryId') or '')==str(draft.get('category_id') or '')),('Listing duration',offer.get('listingDuration'),'GTC',offer.get('listingDuration')=='GTC'),('Quantity',offer.get('availableQuantity'),1,offer.get('availableQuantity')==1),('Price',price.get('value'),draft.get('price'),str(price.get('value'))==str(draft.get('price'))),('Currency',price.get('currency'),'USD',price.get('currency')=='USD'),('Shipping service',service,'StandardShippingFromOutsideUS',service=='StandardShippingFromOutsideUS'),('Shipping cost',cost,exp_ship,ship_ok),('Best Offer',str(((lp.get('bestOfferTerms') or {}).get('bestOfferEnabled'))),('True' if draft.get('best_offer_supported') else 'not required'),((lp.get('bestOfferTerms') or {}).get('bestOfferEnabled') is True) if draft.get('best_offer_supported') else True),('Condition',inv.get('condition'),draft.get('condition_enum'),inv.get('condition')==draft.get('condition_enum')),('EPS photos',len(images),f'1–24 (expected {len(exp_urls)})',1<=len(images)<=24 and len(images)==len(exp_urls)),('Photo order','MATCH' if photo_order_ok else 'MISMATCH','MATCH',photo_order_ok),('Main photo','MATCH' if (photo_order_ok and images and exp_urls and images[0]==exp_urls[0]) else 'MISMATCH','MATCH',photo_order_ok and bool(images)),('Title',product.get('title'),draft.get('title'),product.get('title')==draft.get('title'))]
        rows=''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(('PASS' if ok else 'FAIL'),html.escape(str(n)),html.escape(str(a)),html.escape(str(e))) for n,a,e,ok in checks)
        if not all(x[3] for x in checks):
            return page(label+' not ready',f'<h2>{label} — NOT READY TO PUBLISH</h2><table border="1" cellpadding="5"><tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}</table><p>publishOffer was not called.</p>'),409
        if request.method=='GET':
            return page(label+' final confirmation',f'<h2>{label} — FINAL PUBLISH CONFIRMATION</h2><p><b>All checks passed, including MAIN PHOTO and photo order.</b></p><table border="1" cellpadding="5"><tr><th>Result</th><th>Check</th><th>Actual</th><th>Expected</th></tr>{rows}</table><form method="post"><button type="submit" style="font-size:18px;padding:14px 18px;font-weight:bold">PUBLISH {label} LIVE ON EBAY</button></form>')
        fresh=_lot002_request('GET',f'https://api.ebay.com/sell/inventory/v1/offer/{oid}',headers=_current_headers(token),timeout=30)
        if fresh.status_code!=200 or (fresh.json() or {}).get('status')!='UNPUBLISHED':
            return page(label+' publish stop','<h2>Final re-check failed. publishOffer was not called.</h2>'),409
        pub=requests.post(f'https://api.ebay.com/sell/inventory/v1/offer/{oid}/publish',headers=_current_headers(token,True),timeout=45)
        if pub.status_code not in (200,201):
            return page(label+' publish failed',f'<h2>eBay did not publish {label}</h2><p>HTTP {pub.status_code}</p><pre>{html.escape(pub.text[:8000])}</pre>'),502
        pd=pub.json() if pub.text.strip() else {}; lid=str(pd.get('listingId') or ''); ebay_url=f'https://www.ebay.com/itm/{lid}' if lid else 'https://www.ebay.com/'
        try:
            published_no = int(re.search(r'(\d+)$', sku).group(1))
        except Exception:
            published_no = _current_lot_number()
        next_no = published_no + 1
        # Persist the next lot in the signed session immediately. If Render goes
        # to sleep before the user presses the next button, reopening the site
        # still starts from the correct next lot.
        session['last_published_sku'] = sku
        session['last_published_lot_number'] = published_no
        session['current_lot_number'] = next_no
        session['lot_counter_schema'] = LOT_COUNTER_SCHEMA_VERSION
        return page(label+' PUBLISHED',f'<h2>{label} — PUBLISHED ON EBAY</h2><p>Listing ID: <b>{html.escape(lid)}</b><br>Offer ID: <b>{html.escape(oid)}</b><br>SKU: <b>{sku}</b></p><hr><p><a href="{html.escape(ebay_url,quote=True)}" target="_blank" style="display:inline-block;padding:14px 18px;border:1px solid #333;text-decoration:none;font-size:18px">OPEN LIVE LISTING ON EBAY</a></p><form action="/next-lot" method="post"><button type="submit" style="font-size:20px;padding:15px 20px;font-weight:bold">START LOT {next_no:03d}</button></form>')
    except Exception as exc:
        return page(label+' publish error',f'<h2>{label} publish error</h2><pre>{html.escape(str(exc))}</pre>'),500


@app.route('/next-lot',methods=['POST'])
def next_lot():
    old_picker = session.get('picker_session_id')
    old_sku = session.get('last_published_sku')
    if not old_sku:
        try:
            old_no = int(session.get('last_published_lot_number') or 0)
            old_sku = f'LOT-{old_no:03d}' if old_no else _current_lot_sku()
        except Exception:
            old_sku = _current_lot_sku()
    try:
        LOT002_DRAFT_CACHE.pop(old_picker,None); PHOTO_CACHE.pop(old_picker,None); EBAY_MEDIA_CACHE.pop(old_sku,None)
        for cache in (ANALYSIS_CACHE, IDENTIFICATION_CACHE, MARKET_CACHE, AUTO_FACT_CACHE):
            cache.pop(old_picker,None)
        for k in list(PHOTO_EDIT_CACHE):
            if isinstance(k,tuple) and k and k[0]==old_picker: PHOTO_EDIT_CACHE.pop(k,None)
    except Exception:
        pass
    try:
        discovered = _discover_next_lot_from_ebay()
    except Exception:
        discovered = 4
    try:
        current = int(session.get('current_lot_number') or 4)
    except Exception:
        current = 4
    session['current_lot_number'] = max(4, current, discovered)
    session['lot_counter_schema'] = LOT_COUNTER_SCHEMA_VERSION
    for key in ('picker_session_id','picker_uri','photo_editor_approved','main_photo_index','photo_order','photo_selection_signature','shipping_usd','item_weight','item_dimensions','last_published_sku','last_published_lot_number'):
        session.pop(key,None)
    return redirect('/picker/start?new=1')