import os
import secrets
import html
import base64
import requests

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
    "temporary-development-key",
)

# ==================================================
# CONFIG
# ==================================================

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

REDIRECT_URI = (
    "https://vintage-ua-ebay-1.onrender.com/google/callback"
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

OPENAI_RESPONSES_URL = (
    "https://api.openai.com/v1/responses"
)

# Temporary memory.
# Photos are NOT permanently stored on Render.
PHOTO_CACHE = {}

# AI result is also temporary for now.
ANALYSIS_CACHE = {}


# ==================================================
# HELPERS
# ==================================================

def google_headers():
    token = session.get("google_access_token")

    return {
        "Authorization": f"Bearer {token}",
    }


def escape_multiline(text):
    return html.escape(text).replace("\n", "<br>")


def extract_openai_text(data):
    """
    Extract text from OpenAI Responses API result.
    """

    # Some API responses may expose output_text
    # directly.
    direct_text = data.get("output_text")

    if direct_text:
        return direct_text

    texts = []

    for output_item in data.get("output", []):

        if output_item.get("type") != "message":
            continue

        for content_item in output_item.get(
            "content",
            [],
        ):

            if content_item.get("type") == "output_text":

                text = content_item.get("text")

                if text:
                    texts.append(text)

    return "\n".join(texts)


# ==================================================
# HOME
# ==================================================

@app.route("/")
def home():

    connected = bool(
        session.get("google_access_token")
    )

    openai_ready = bool(
        OPENAI_API_KEY
    )

    if connected:

        google_section = """
        <p style="color:green;">
            <b>Google Photos connected ✓</b>
        </p>

        <p>
            <a href="/picker/start">
                <button style="
                    font-size:20px;
                    padding:12px 20px;">
                    Select LOT photos
                </button>
            </a>
        </p>
        """

    else:

        google_section = """
        <p>
            <a href="/google/login">
                <button style="
                    font-size:20px;
                    padding:12px 20px;">
                    Connect Google Photos
                </button>
            </a>
        </p>
        """

    if openai_ready:

        ai_status = """
        <p style="color:green;">
            <b>OpenAI API connected ✓</b>
        </p>
        """

    else:

        ai_status = """
        <p style="color:red;">
            <b>OpenAI API key missing</b>
        </p>
        """

    return f"""
    <!doctype html>

    <html>
    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>Vintage UA eBay</title>

    </head>

    <body style="
        font-family:Arial;
        padding:20px;
        max-width:1000px;
        margin:auto;">

        <h2>Vintage UA eBay</h2>

        <p>Server is running ✓</p>

        {google_section}

        {ai_status}

    </body>
    </html>
    """


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


# ==================================================
# GOOGLE OAUTH
# ==================================================

@app.route("/google/login")
def google_login():

    if not GOOGLE_CLIENT_ID:

        return (
            "GOOGLE_CLIENT_ID is not configured.",
            500,
        )

    state = secrets.token_urlsafe(32)

    session["google_oauth_state"] = state

    params = {

        "client_id":
            GOOGLE_CLIENT_ID,

        "redirect_uri":
            REDIRECT_URI,

        "response_type":
            "code",

        "scope":
            PICKER_SCOPE,

        "access_type":
            "offline",

        "prompt":
            "consent",

        "state":
            state,
    }

    prepared = requests.Request(
        "GET",
        GOOGLE_AUTH_URL,
        params=params,
    ).prepare()

    return redirect(prepared.url)


@app.route("/google/callback")
def google_callback():

    error = request.args.get("error")

    if error:

        return (
            f"Google authorization error: "
            f"{html.escape(error)}",
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
            "Authorization code was not returned.",
            400,
        )

    token_response = requests.post(

        GOOGLE_TOKEN_URL,

        data={

            "code":
                code,

            "client_id":
                GOOGLE_CLIENT_ID,

            "client_secret":
                GOOGLE_CLIENT_SECRET,

            "redirect_uri":
                REDIRECT_URI,

            "grant_type":
                "authorization_code",
        },

        timeout=30,
    )

    if not token_response.ok:

        return (
            "Google token exchange failed."
            "<br><br>"
            + html.escape(
                token_response.text
            ),
            500,
        )

    token_data = token_response.json()

    session["google_access_token"] = (
        token_data["access_token"]
    )

    if token_data.get("refresh_token"):

        session["google_refresh_token"] = (
            token_data["refresh_token"]
        )

    return redirect("/")


# ==================================================
# CREATE GOOGLE PHOTOS PICKER SESSION
# ==================================================

@app.route("/picker/start")
def picker_start():

    if not session.get(
        "google_access_token"
    ):

        return redirect(
            "/google/login"
        )

    response = requests.post(

        f"{PICKER_API}/sessions",

        headers={
            **google_headers(),
            "Content-Type": "application/json",
        },

        json={},

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
            "Could not create Google Photos "
            "Picker session."
            "<br><br>"
            + html.escape(
                response.text
            ),
            500,
        )

    data = response.json()

    picker_session_id = data.get("id")
    picker_uri = data.get("pickerUri")

    if not picker_session_id:

        return (
            "Google did not return "
            "Picker session ID.",
            500,
        )

    if not picker_uri:

        return (
            "Google did not return "
            "Picker URI.",
            500,
        )

    session["picker_session_id"] = (
        picker_session_id
    )

    return f"""
    <!doctype html>

    <html>
    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>Select LOT photos</title>

    </head>

    <body style="
        font-family:Arial;
        padding:20px;
        max-width:900px;
        margin:auto;">

        <h2>LOT 001</h2>

        <p>
            <b>Step 1.</b>
            Select exactly
            <b>24 photos</b>.
        </p>

        <p>

            <a
                href="{html.escape(picker_uri)}"
                target="_blank">

                <button style="
                    font-size:20px;
                    padding:12px 20px;">

                    Open Google Photos

                </button>

            </a>

        </p>

        <hr>

        <p>
            <b>Step 2.</b>
            After Google Photos says
            <b>Done</b>,
            return here.
        </p>

        <p>

            <a href="/picker/items">

                <button style="
                    font-size:20px;
                    padding:12px 20px;">

                    I selected the photos —
                    Continue

                </button>

            </a>

        </p>

    </body>
    </html>
    """


# ==================================================
# GET SELECTED ITEMS
# ==================================================

def get_selected_items():

    picker_session_id = session.get(
        "picker_session_id"
    )

    if not picker_session_id:

        return None, "No Picker session."

    items = []
    page_token = None

    while True:

        params = {
            "sessionId": picker_session_id,
            "pageSize": 100,
        }

        if page_token:

            params["pageToken"] = (
                page_token
            )

        response = requests.get(

            f"{PICKER_API}/mediaItems",

            headers=google_headers(),

            params=params,

            timeout=30,
        )

        if not response.ok:

            return None, response.text

        data = response.json()

        items.extend(
            data.get(
                "mediaItems",
                []
            )
        )

        page_token = data.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return items, None


@app.route("/picker/items")
def picker_items():

    if not session.get(
        "google_access_token"
    ):

        return redirect(
            "/google/login"
        )

    picker_session_id = session.get(
        "picker_session_id"
    )

    if not picker_session_id:

        return redirect(
            "/picker/start"
        )

    items, error = get_selected_items()

    if error:

        return (
            "Could not get selected photos."
            "<br><br>"
            + html.escape(error),
            500,
        )

    if not items:

        return """
        <html>
        <head>
            <meta
                name="viewport"
                content="width=device-width,
                initial-scale=1">
        </head>

        <body style="
            font-family:Arial;
            padding:20px;">

            <h2>Waiting for photos</h2>

            <p>
                Google returned zero photos.
            </p>

            <a href="/picker/items">
                <button>
                    Check again
                </button>
            </a>

        </body>
        </html>
        """

    PHOTO_CACHE[picker_session_id] = items

    count = len(items)

    cards = ""

    for index, item in enumerate(
        items,
        start=1,
    ):

        media_file = item.get(
            "mediaFile",
            {}
        )

        filename = html.escape(
            media_file.get(
                "filename",
                f"Photo {index}",
            )
        )

        mime_type = html.escape(
            media_file.get(
                "mimeType",
                "",
            )
        )

        cards += f"""
        <div style="
            border:1px solid #ccc;
            border-radius:8px;
            padding:8px;
            text-align:center;
            background:#fff;">

            <img
                src="/picker/thumb/{index}"
                alt="{filename}"
                loading="lazy"
                style="
                    width:100%;
                    height:180px;
                    object-fit:contain;
                    background:#f3f3f3;
                    border-radius:5px;">

            <div style="
                margin-top:7px;
                font-size:13px;
                word-break:break-word;">

                <b>{index}.</b>
                {filename}

            </div>

            <div style="
                font-size:11px;
                color:#666;">

                {mime_type}

            </div>

        </div>
        """

    if count == 24:

        status = (
            "✓ Correct — 24 photos selected."
        )

        status_color = "green"

        analyze_button = """
        <form
            action="/picker/analyze"
            method="post"
            style="margin-top:25px;">

            <button
                type="submit"
                style="
                    font-size:20px;
                    padding:14px 22px;
                    font-weight:bold;">

                Analyze LOT 001 with AI

            </button>

        </form>
        """

    else:

        status = (
            f"Attention: expected "
            f"24 photos, received "
            f"{count}."
        )

        status_color = "red"

        analyze_button = ""

    return f"""
    <!doctype html>

    <html>
    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>LOT 001</title>

    </head>

    <body style="
        font-family:Arial;
        padding:15px;
        max-width:1200px;
        margin:auto;
        background:#fafafa;">

        <h2>LOT 001</h2>

        <h3>
            Photos: {count}/24
        </h3>

        <p style="
            color:{status_color};
            font-size:18px;">

            <b>{status}</b>

        </p>

        <div style="
            display:grid;
            grid-template-columns:
                repeat(
                    auto-fill,
                    minmax(150px, 1fr)
                );
            gap:12px;">

            {cards}

        </div>

        {analyze_button}

        <p style="margin-top:25px;">

            <a href="/">
                Return home
            </a>

        </p>

    </body>
    </html>
    """


# ==================================================
# THUMBNAIL
# ==================================================

@app.route("/picker/thumb/<int:index>")
def picker_thumb(index):

    token = session.get(
        "google_access_token"
    )

    picker_session_id = session.get(
        "picker_session_id"
    )

    if not token:

        return (
            "Google token missing.",
            401,
        )

    items = PHOTO_CACHE.get(
        picker_session_id
    )

    if not items:

        return (
            "Photo cache expired.",
            404,
        )

    if index < 1 or index > len(items):

        return (
            "Photo not found.",
            404,
        )

    item = items[index - 1]

    media_file = item.get(
        "mediaFile",
        {}
    )

    base_url = media_file.get(
        "baseUrl"
    )

    if not base_url:

        return (
            "baseUrl missing.",
            404,
        )

    image_url = (
        base_url
        + "=w600-h600"
    )

    response = requests.get(

        image_url,

        headers={
            "Authorization":
                f"Bearer {token}"
        },

        timeout=30,
    )

    if not response.ok:

        return (
            "Could not download image.",
            response.status_code,
        )

    content_type = response.headers.get(
        "Content-Type",
        "image/jpeg",
    )

    return Response(
        response.content,
        content_type=content_type,
    )


# ==================================================
# DOWNLOAD PHOTO FOR AI
# ==================================================

def google_photo_to_data_url(item):

    token = session.get(
        "google_access_token"
    )

    media_file = item.get(
        "mediaFile",
        {}
    )

    base_url = media_file.get(
        "baseUrl"
    )

    if not base_url:

        raise Exception(
            "Google photo baseUrl missing."
        )

    # We do not need full 12–20 MP photos for
    # the first AI pass.
    # 1024 px keeps the request much smaller.
    image_url = (
        base_url
        + "=w1024-h1024"
    )

    response = requests.get(

        image_url,

        headers={
            "Authorization":
                f"Bearer {token}"
        },

        timeout=45,
    )

    if not response.ok:

        raise Exception(
            "Google image download failed: "
            + response.text[:300]
        )

    content_type = response.headers.get(
        "Content-Type",
        media_file.get(
            "mimeType",
            "image/jpeg",
        ),
    )

    encoded = base64.b64encode(
        response.content
    ).decode("utf-8")

    return (
        f"data:{content_type};"
        f"base64,{encoded}"
    )


# ==================================================
# AI ANALYSIS
# ==================================================

@app.route(
    "/picker/analyze",
    methods=["POST"],
)
def picker_analyze():

    picker_session_id = session.get(
        "picker_session_id"
    )

    items = PHOTO_CACHE.get(
        picker_session_id,
        [],
    )

    if len(items) != 24:

        return """
        <!doctype html>
        <html>
        <head>
            <meta
                name="viewport"
                content="width=device-width,
                initial-scale=1">
        </head>

        <body style="
            font-family:Arial;
            padding:20px;">

            <h2>LOT photos expired</h2>

            <p>
                The temporary Render photo
                cache does not contain
                24 photos.
            </p>

            <p>
                Select the 24 photos again.
            </p>

            <a href="/picker/start">
                <button>
                    Select LOT photos
                </button>
            </a>

        </body>
        </html>
        """, 400

    if not OPENAI_API_KEY:

        return (
            "OPENAI_API_KEY is not configured "
            "in Render.",
            500,
        )

    content = []

    prompt = """
You are analysing a vintage or collectible item
for an eBay.com seller in Ukraine.

Study ALL 24 photographs together.
They are photographs of ONE single lot.

Do not invent manufacturer names, model numbers,
dates, materials, markings, completeness, defects
or authenticity.

Clearly separate:
- what is directly visible,
- what is likely,
- what cannot yet be confirmed.

Pay special attention to:
manufacturer or brand,
country of manufacture,
approximate production period,
model or family,
markings and logos,
materials,
mechanism,
condition,
damage,
repairs,
missing parts,
completeness,
and anything affecting eBay value.

The seller needs a conservative professional
assessment, not advertising language.

Return the answer IN UKRAINIAN.

Use exactly these sections:

LOT 001 — ПОПЕРЕДНІЙ AI-АНАЛІЗ

1. ЩО ЦЕ
2. ВИРОБНИК / БРЕНД
3. КРАЇНА ТА ПЕРІОД
4. МОДЕЛЬ / МОДИФІКАЦІЯ
5. МАРКУВАННЯ ТА НАПИСИ
6. КОМПЛЕКТНІСТЬ
7. СТАН
8. ВИДИМІ ДЕФЕКТИ
9. ЩО ПОТРІБНО ПЕРЕВІРИТИ ВРУЧНУ
10. ЯКИХ ФОТО НЕ ВИСТАЧАЄ
11. ПОПЕРЕДНЯ ОЦІНКА ВПЕВНЕНОСТІ 0–100%
12. НАСТУПНИЙ КРОК

Do NOT estimate the selling price yet.
Do NOT claim that you searched eBay.
Market research will be done separately.
"""

    content.append({
        "type": "input_text",
        "text": prompt,
    })

    try:

        for item in items:

            data_url = (
                google_photo_to_data_url(
                    item
                )
            )

            content.append({
                "type": "input_image",
                "image_url": data_url,
                "detail": "low",
            })

    except Exception as exc:

        return (
            "<h2>Photo download error</h2>"
            "<pre>"
            + html.escape(str(exc))
            + "</pre>",
            500,
        )

    payload = {

        "model":
            "gpt-5.6-luna",

        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],

        "max_output_tokens": 2200,

        "store": False,
    }

    try:

        response = requests.post(

            OPENAI_RESPONSES_URL,

            headers={
                "Authorization":
                    f"Bearer {OPENAI_API_KEY}",

                "Content-Type":
                    "application/json",
            },

            json=payload,

            timeout=240,
        )

    except Exception as exc:

        return (
            "<h2>OpenAI connection error</h2>"
            "<pre>"
            + html.escape(str(exc))
            + "</pre>",
            500,
        )

    if not response.ok:

        return f"""
        <!doctype html>
        <html>
        <head>
            <meta
                name="viewport"
                content="width=device-width,
                initial-scale=1">
        </head>

        <body style="
            font-family:Arial;
            padding:20px;">

            <h2>OpenAI API error</h2>

            <p>
                HTTP status:
                <b>{response.status_code}</b>
            </p>

            <pre style="
                white-space:pre-wrap;
                word-break:break-word;">
{html.escape(response.text[:5000])}
            </pre>

            <p>
                <a href="/picker/items">
                    Back to photos
                </a>
            </p>

        </body>
        </html>
        """, response.status_code

    data = response.json()

    result_text = extract_openai_text(
        data
    )

    if not result_text:

        return (
            "<h2>OpenAI returned no text</h2>"
            "<pre>"
            + html.escape(
                str(data)[:5000]
            )
            + "</pre>",
            500,
        )

    ANALYSIS_CACHE[
        picker_session_id
    ] = result_text

    result_html = escape_multiline(
        result_text
    )

    return f"""
    <!doctype html>

    <html>
    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>LOT 001 AI Analysis</title>

    </head>

    <body style="
        font-family:Arial;
        padding:18px;
        max-width:900px;
        margin:auto;
        line-height:1.55;">

        <h2>
            LOT 001 — AI analysis
        </h2>

        <p style="color:green;">
            <b>
                ✓ 24 photos analysed
            </b>
        </p>

        <div style="
            border:1px solid #ccc;
            border-radius:10px;
            padding:18px;
            background:#fafafa;">

            {result_html}

        </div>

        <p style="
            margin-top:25px;">

            <a href="/picker/items">
                Back to photos
            </a>

        </p>

        <p>
            Next stage:
            identification verification
            and eBay market research.
        </p>

    </body>
    </html>
    """


# Old route kept only so old links do not break.
@app.route("/picker/prepare")
def picker_prepare():

    return redirect(
        "/picker/items"
    )


# ==================================================
# START
# ==================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000,
            )
        ),
    )
