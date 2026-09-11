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
    "temporary-development-key"
)

# =========================================================
# CONFIG
# =========================================================

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

OPENAI_URL = (
    "https://api.openai.com/v1/responses"
)

PHOTO_CACHE = {}


# =========================================================
# HTML HELPERS
# =========================================================

def page(title, body):

    return f"""
    <!doctype html>

    <html>

    <head>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1">

        <title>
            {html.escape(title)}
        </title>

    </head>

    <body style="
        font-family:Arial,sans-serif;
        padding:18px;
        max-width:1000px;
        margin:auto;
        line-height:1.55;">

        {body}

    </body>

    </html>
    """


def text_to_html(text):

    return (
        html.escape(text)
        .replace("\n", "<br>")
    )


def hidden_field(name, value):

    return (
        f'<textarea '
        f'name="{html.escape(name)}" '
        f'style="display:none;">'
        f'{html.escape(value)}'
        f'</textarea>'
    )


# =========================================================
# OPENAI HELPERS
# =========================================================

def extract_openai_text(data):

    direct = data.get(
        "output_text"
    )

    if direct:
        return direct

    texts = []

    for item in data.get(
        "output",
        []
    ):

        if item.get("type") != "message":
            continue

        for content in item.get(
            "content",
            []
        ):

            if (
                content.get("type")
                == "output_text"
            ):

                text = content.get(
                    "text"
                )

                if text:
                    texts.append(text)

    return "\n".join(texts)


def call_openai(
    payload,
    timeout=270
):

    response = requests.post(

        OPENAI_URL,

        headers={
            "Authorization":
                f"Bearer {OPENAI_API_KEY}",

            "Content-Type":
                "application/json",
        },

        json=payload,

        timeout=timeout,
    )

    if not response.ok:

        raise RuntimeError(
            f"OpenAI HTTP "
            f"{response.status_code}\n"
            + response.text[:5000]
        )

    data = response.json()

    result = (
        extract_openai_text(
            data
        )
    )

    if not result:

        raise RuntimeError(
            "OpenAI returned no text.\n"
            + str(data)[:5000]
        )

    return result


# =========================================================
# GOOGLE HELPERS
# =========================================================

def google_headers():

    token = session.get(
        "google_access_token"
    )

    return {
        "Authorization":
            f"Bearer {token}"
    }


def get_selected_items():

    picker_id = session.get(
        "picker_session_id"
    )

    if not picker_id:

        return (
            None,
            "No Picker session."
        )

    items = []
    page_token = None

    while True:

        params = {
            "sessionId":
                picker_id,

            "pageSize":
                100,
        }

        if page_token:

            params[
                "pageToken"
            ] = page_token

        response = requests.get(

            f"{PICKER_API}/mediaItems",

            headers=google_headers(),

            params=params,

            timeout=30,
        )

        if not response.ok:

            return (
                None,
                response.text
            )

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


def google_photo_to_data_url(
    item,
    size=1024
):

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
            "Authorization":
                f"Bearer {token}"
        },

        timeout=45,
    )

    if not response.ok:

        raise RuntimeError(
            "Google image download failed. "
            f"HTTP {response.status_code}"
        )

    content_type = (
        response.headers.get(
            "Content-Type",
            media_file.get(
                "mimeType",
                "image/jpeg"
            )
        )
    )

    encoded = base64.b64encode(
        response.content
    ).decode(
        "utf-8"
    )

    return (
        f"data:{content_type};"
        f"base64,{encoded}"
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    google_connected = bool(
        session.get(
            "google_access_token"
        )
    )

    if google_connected:

        google_block = """
        <p style="color:green;">
            <b>
                Google Photos connected ✓
            </b>
        </p>

        <p>
            <a href="/picker/start">

                <button style="
                    font-size:20px;
                    padding:12px 18px;">

                    Start LOT 001

                </button>

            </a>
        </p>
        """

    else:

        google_block = """
        <p>
            <a href="/google/login">

                <button style="
                    font-size:20px;
                    padding:12px 18px;">

                    Connect Google Photos

                </button>

            </a>
        </p>
        """

    if OPENAI_API_KEY:

        openai_block = """
        <p style="color:green;">
            <b>
                OpenAI API connected ✓
            </b>
        </p>
        """

    else:

        openai_block = """
        <p style="color:red;">
            <b>
                OpenAI API key missing
            </b>
        </p>
        """

    body = f"""
    <h2>
        Vintage UA eBay
    </h2>

    <p>
        Server is running ✓
    </p>

    {google_block}

    {openai_block}

    <hr>

    <h3>
        LOT workflow
    </h3>

    <p>
        24 photos
        →
        visual AI analysis
        →
        visual + web identification
        →
        eBay market research
        →
        shipping
        →
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

        "status":
            "ok",

        "google_configured":
            bool(
                GOOGLE_CLIENT_ID
                and
                GOOGLE_CLIENT_SECRET
            ),

        "openai_configured":
            bool(
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
            500
        )

    state = secrets.token_urlsafe(
        32
    )

    session[
        "google_oauth_state"
    ] = state

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

    return redirect(
        prepared.url
    )


@app.route("/google/callback")
def google_callback():

    if request.args.get(
        "error"
    ):

        return (
            "Google authorization error: "
            + html.escape(
                request.args.get(
                    "error"
                )
            ),
            400,
        )

    received_state = request.args.get(
        "state"
    )

    saved_state = session.pop(
        "google_oauth_state",
        None
    )

    if (
        not saved_state
        or
        received_state != saved_state
    ):

        return (
            "Invalid OAuth state.",
            400,
        )

    code = request.args.get(
        "code"
    )

    if not code:

        return (
            "Authorization code missing.",
            400,
        )

    response = requests.post(

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

    if not response.ok:

        return (
            "Google token exchange failed."
            "<br><pre>"
            + html.escape(
                response.text
            )
            + "</pre>",
            500,
        )

    token_data = response.json()

    session[
        "google_access_token"
    ] = token_data[
        "access_token"
    ]

    if token_data.get(
        "refresh_token"
    ):

        session[
            "google_refresh_token"
        ] = token_data[
            "refresh_token"
        ]

    return redirect("/")


# =========================================================
# PICKER START
# =========================================================

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

            "Content-Type":
                "application/json",
        },

        json={},

        timeout=30,
    )

    if response.status_code == 401:

        session.pop(
            "google_access_token",
            None
        )

        return redirect(
            "/google/login"
        )

    if not response.ok:

        return (
            "Could not create "
            "Picker session."
            "<br><pre>"
            + html.escape(
                response.text
            )
            + "</pre>",
            500,
        )

    data = response.json()

    picker_id = data.get(
        "id"
    )

    picker_uri = data.get(
        "pickerUri"
    )

    if (
        not picker_id
        or
        not picker_uri
    ):

        return (
            "Google Picker session "
            "data missing.",
            500,
        )

    session[
        "picker_session_id"
    ] = picker_id

    body = f"""
    <h2>
        LOT 001
    </h2>

    <h3>
        Step 1
    </h3>

    <p>
        Select exactly
        <b>24 photos</b>
        of ONE item.
    </p>

    <p>

        <a
            href="{html.escape(picker_uri)}"
            target="_blank">

            <button style="
                font-size:20px;
                padding:12px 18px;">

                Open Google Photos

            </button>

        </a>

    </p>

    <hr>

    <h3>
        Step 2
    </h3>

    <p>
        After selection is complete,
        return here.
    </p>

    <p>

        <a href="/picker/items">

            <button style="
                font-size:20px;
                padding:12px 18px;">

                Continue

            </button>

        </a>

    </p>
    """

    return page(
        "LOT 001",
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

    items, error = (
        get_selected_items()
    )

    if error:

        return (
            "Could not get selected photos."
            "<br><pre>"
            + html.escape(
                error
            )
            + "</pre>",
            500,
        )

    if not items:

        return page(
            "No photos",
            """
            <h2>
                No photos yet
            </h2>

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

    PHOTO_CACHE[
        picker_id
    ] = items

    count = len(
        items
    )

    cards = ""

    for index, item in enumerate(
        items,
        start=1
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
            text-align:center;">

            <img
                src="/picker/thumb/{index}"
                loading="lazy"
                style="
                    width:100%;
                    height:160px;
                    object-fit:contain;">

            <div style="
                font-size:11px;
                margin-top:5px;">

                {index}.
                {filename}

            </div>

        </div>
        """

    if count == 24:

        status = """
        <p style="color:green;">
            <b>
                ✓ Correct —
                24 photos selected
            </b>
        </p>
        """

        button = """
        <form
            action="/picker/analyze"
            method="post">

            <button
                type="submit"
                style="
                    font-size:20px;
                    padding:14px 20px;
                    margin-top:20px;">

                Analyze LOT 001 with AI

            </button>

        </form>
        """

    else:

        status = f"""
        <p style="color:red;">
            <b>
                Expected 24 photos.
                Selected: {count}
            </b>
        </p>
        """

        button = ""

    body = f"""
    <h2>
        LOT 001
    </h2>

    <h3>
        Photos:
        {count}/24
    </h3>

    {status}

    <div style="
        display:grid;
        grid-template-columns:
            repeat(
                auto-fill,
                minmax(135px,1fr)
            );
        gap:10px;">

        {cards}

    </div>

    {button}

    <p style="
        margin-top:25px;">

        <a href="/">
            Return home
        </a>

    </p>
    """

    return page(
        "LOT 001 Photos",
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
        or
        not items
    ):

        return (
            "Photo cache expired",
            404,
        )

    if (
        index < 1
        or
        index > len(items)
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

        base_url
        + "=w600-h600",

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

        content_type=
            response.headers.get(
                "Content-Type",
                "image/jpeg"
            )
    )


# =========================================================
# STEP 1 — VISUAL ANALYSIS
# =========================================================

@app.route(
    "/picker/analyze",
    methods=["POST"]
)
def picker_analyze():

    picker_id = session.get(
        "picker_session_id"
    )

    items = PHOTO_CACHE.get(
        picker_id,
        []
    )

    if len(items) != 24:

        return page(
            "Photos expired",
            """
            <h2>
                Photos expired
            </h2>

            <p>
                Select the 24 photos again.
            </p>

            <a href="/picker/start">
                Start again
            </a>
            """
        ), 400

    if not OPENAI_API_KEY:

        return (
            "OPENAI_API_KEY missing",
            500,
        )

    prompt = """
You are analysing ONE vintage collectible item
for an eBay.com seller.

Study ALL 24 photographs together.

Do not invent information.

Carefully inspect every photograph for:
- manufacturer marks
- factory marks
- country marks
- logos
- serial numbers
- labels
- stamps
- quality marks
- model numbers
- text
- mechanism construction

IMPORTANT:
A quality mark, inspection mark or state symbol
is NOT automatically a manufacturer's logo.

Separate:
- directly visible facts
- likely conclusions
- unconfirmed possibilities

Pay attention to:
object type,
manufacturer,
brand,
country,
period,
model,
materials,
mechanism,
completeness,
missing parts,
condition,
damage,
repairs,
modifications.

Return IN UKRAINIAN.

Use:

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

11. ОЦІНКА ВПЕВНЕНОСТІ 0–100%

12. НАСТУПНИЙ КРОК

Do not research market prices yet.
"""

    content = [
        {
            "type":
                "input_text",

            "text":
                prompt,
        }
    ]

    try:

        for item in items:

            content.append({
                "type":
                    "input_image",

                "image_url":
                    google_photo_to_data_url(
                        item,
                        1024
                    ),

                "detail":
                    "low",
            })

        analysis = call_openai(

            {
                "model":
                    "gpt-5.6-luna",

                "input": [
                    {
                        "role":
                            "user",

                        "content":
                            content,
                    }
                ],

                "reasoning": {
                    "effort":
                        "low"
                },

                "max_output_tokens":
                    3000,

                "store":
                    False,
            },

            timeout=270,
        )

    except Exception as exc:

        return page(
            "AI Error",
            f"""
            <h2>
                AI analysis error
            </h2>

            <pre style="
                white-space:pre-wrap;">
{html.escape(str(exc))}
            </pre>

            <a href="/picker/items">
                Back
            </a>
            """
        ), 500

    body = f"""
    <h2>
        LOT 001 —
        AI analysis
    </h2>

    <p style="color:green;">
        <b>
            ✓ 24 photos analyzed
        </b>
    </p>

    <div style="
        border:1px solid #
