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
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
EBAY_RUNAME = os.environ.get("EBAY_RUNAME")
EBAY_AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
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
    return "eBay connected successfully!"

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


def call_openai(payload, timeout=270):
    response = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if not response.ok:
        raise RuntimeError(
            f"OpenAI HTTP {response.status_code}\n"
            + response.text[:5000]
        )

    data = response.json()

    result = extract_openai_text(data)

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
    token = session.get("google_access_token")

    return {
        "Authorization": f"Bearer {token}"
    }


def get_selected_items():
    picker_id = session.get("picker_session_id")

    if not picker_id:
        return None, "No Picker session."

    items = []
    page_token = None

    while True:
        params = {
            "sessionId": picker_id,
            "pageSize": 100,
        }

        if page_token:
            params["pageToken"] = page_token

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
            data.get("mediaItems", [])
        )

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


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    google_connected = bool(
        session.get("google_access_token")
    )

    if google_connected:
        google_block = """
        <p style="color:green;">
            <b>Google Photos connected ✓</b>
        </p>

        <p>
            <a href="/picker/start">
                <button style="
                    font-size:20px;
                    padding:12px 18px;
                ">
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
        24 photos →
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
            "Could not create Picker session."
            "<br><pre>"
            + html.escape(response.text)
            + "</pre>",
            500,
        )

    data = response.json()

    picker_id = data.get("id")
    picker_uri = data.get("pickerUri")

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

    body = f"""
    <h2>LOT 001</h2>

    <h3>Step 1</h3>

    <p>
        Select exactly
        <b>24 photos</b>
        of ONE item.
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

    items, error = get_selected_items()

    if error:
        return (
            "Could not get selected photos."
            "<br><pre>"
            + html.escape(error)
            + "</pre>",
            500,
        )

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

    PHOTO_CACHE[picker_id] = items

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
                src="/picker/thumb/{index}"
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
            method="post"
        >
            <button
                type="submit"
                style="
                    font-size:20px;
                    padding:14px 20px;
                    margin-top:20px;
                "
            >
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
    <h2>LOT 001</h2>

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
            <h2>Photos expired</h2>

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
            "type": "input_text",
            "text": prompt,
        }
    ]

    try:
        for item in items:
            content.append({
                "type": "input_image",
                "image_url":
                    google_photo_to_data_url(
                        item,
                        1024
                    ),
                "detail": "low",
            })

        analysis = call_openai(
            {
                "model":
                    "gpt-5.6-luna",

                "input": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],

                "reasoning": {
                    "effort": "low"
                },

                "max_output_tokens":
                    3000,

                "store": False,
            },
            timeout=270,
        )

    except Exception as exc:
        return page(
            "AI Error",
            f"""
            <h2>AI analysis error</h2>

            <pre style="
                white-space:pre-wrap;
                word-break:break-word;
            ">
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
        border:1px solid #ccc;
        padding:16px;
        border-radius:8px;
        background:#fafafa;
    ">
        {text_to_html(analysis)}
    </div>

    <form
        action="/picker/research-identification"
        method="post"
        style="margin-top:25px;"
    >
        {hidden_field(
            "analysis",
            analysis
        )}

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;
            "
        >
            Step 2 —
            Visual + Web Identification
        </button>
    </form>
    """

    return page(
        "LOT 001 AI Analysis",
        body
    )


# =========================================================
# STEP 2 — VISUAL + WEB IDENTIFICATION
# =========================================================

@app.route(
    "/picker/research-identification",
    methods=["POST"],
)
def research_identification():
    analysis = request.form.get(
        "analysis",
        ""
    )

    if not analysis:
        return page(
            "Missing analysis",
            """
            <h2>AI analysis missing</h2>

            <p>
                Run photo analysis again.
            </p>
            """
        ), 400

    picker_id = session.get(
        "picker_session_id"
    )

    items = PHOTO_CACHE.get(
        picker_id,
        []
    )

    if len(items) != 24:
        return page(
            "Photos unavailable",
            """
            <h2>
                Original photos are no longer
                available in temporary memory.
            </h2>

            <p>
                Select LOT 001 photos again.
            </p>

            <a href="/picker/start">
                Select photos again
            </a>
            """
        ), 400

    identification_prompt = f"""
You are performing expert identification of
ONE vintage collectible item.

You have BOTH:
1. The previous visual AI analysis.
2. The original 24 photographs.
3. Web Search.

Do NOT trust the previous analysis blindly.

PREVIOUS ANALYSIS:

--------------------------------

{analysis}

--------------------------------

Independently inspect the photographs again
and verify identification with web research.

CRITICAL RULES:

1. VISUAL EVIDENCE OVERRIDES GENERIC WEB MATCHES.

2. Do not identify an item only because its
general silhouette resembles a common product.

3. Compare exact details:
- front case shape
- back case construction
- dial
- hands
- pendulum
- chains
- weights
- movement layout
- bellows
- doors
- internal parts
- screws
- materials
- decorative pattern
- stamps
- logos
- labels
- quality marks

4. Carefully inspect symbols and stamps.

A quality mark, inspection mark, certification
mark or government symbol is NOT automatically
a manufacturer's logo.

5. Determine whether visible marks indicate:
- USSR / Soviet origin
- Germany / West Germany
- Switzerland
- another country

Do not assume Germany simply because the object
is a cuckoo clock.

6. Search multiple competing hypotheses.

For every serious candidate ask:
"Do the photographs actually match this maker?"

7. If a manufacturer cannot be confirmed,
say so.

8. Never invent:
- manufacturer
- factory
- model
- catalog number
- production year
- country
- marking interpretation

9. Search for visually matching examples,
including:
- auction archives
- collector references
- museum references
- vintage catalogues
- specialist sites
- eBay examples
- other marketplaces when useful

10. A generic history page is NOT evidence
that this exact item was produced by that maker.

Return IN UKRAINIAN.

Use:

LOT 001 — ПЕРЕВІРЕНА ІДЕНТИФІКАЦІЯ

1. ЩО ЦЕ

2. НАЙІМОВІРНІШИЙ ВИРОБНИК / ФАБРИКА

3. КРАЇНА ПОХОДЖЕННЯ

4. ПЕРІОД ВИРОБНИЦТВА

5. МОДЕЛЬ / СЕРІЯ

6. АНАЛІЗ МАРКУВАНЬ

For each visible symbol explain whether it is:
- manufacturer mark
- quality/certification mark
- inspection mark
- unknown

7. ВІЗУАЛЬНІ ДОКАЗИ

8. ВЕБ-ДОКАЗИ

9. ПОРІВНЯННЯ З АЛЬТЕРНАТИВНИМИ ВЕРСІЯМИ

10. ЩО ТОЧНО ПІДТВЕРДЖЕНО

11. ЩО ЗАЛИШАЄТЬСЯ НЕПІДТВЕРДЖЕНИМ

12. РІВЕНЬ ВПЕВНЕНОСТІ 0–100%

Give separate confidence for:
- item type
- country
- manufacturer/factory
- period
- exact model

13. ЧИ ПОТРІБНА РУЧНА ПЕРЕВІРКА

14. ЯКІ ДОДАТКОВІ ФОТО ПОТРІБНІ

15. ОСНОВНІ ДЖЕРЕЛА

Do NOT research market price in this step.

Accuracy is more important than confidence.
"""

    content = [
        {
            "type": "input_text",
            "text":
                identification_prompt,
        }
    ]

    try:
        for item in items:
            content.append({
                "type": "input_image",
                "image_url":
                    google_photo_to_data_url(
                        item,
                        1024
                    ),
                "detail": "low",
            })

        identification = call_openai(
            {
                "model":
                    "gpt-5.6-terra",

                "tools": [
                    {
                        "type":
                            "web_search"
                    }
                ],

                "input": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],

                "reasoning": {
                    "effort": "medium"
                },

                "max_output_tokens":
                    4500,

                "store": False,
            },
            timeout=270,
        )

    except Exception as exc:
        return page(
            "Identification Error",
            f"""
            <h2>
                Identification research error
            </h2>

            <pre style="
                white-space:pre-wrap;
                word-break:break-word;
            ">
{html.escape(str(exc))}
            </pre>

            <p>
                Use Back in the browser.
            </p>
            """
        ), 500

    body = f"""
    <h2>
        LOT 001 —
        Verified Identification
    </h2>

    <p style="color:green;">
        <b>
            ✓ 24 photos + Web Search analyzed
        </b>
    </p>

    <div style="
        border:1px solid #ccc;
        padding:16px;
        border-radius:8px;
        background:#fafafa;
    ">
        {text_to_html(
            identification
        )}
    </div>

    <form
        action="/picker/research-market"
        method="post"
        style="margin-top:25px;"
    >
        {hidden_field(
            "analysis",
            analysis
        )}

        {hidden_field(
            "identification",
            identification
        )}

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;
            "
        >
            Step 3 —
            Research eBay Market
        </button>
    </form>
    """

    return page(
        "LOT 001 Identification",
        body
    )


# =========================================================
# STEP 3 — EBAY MARKET
# =========================================================

@app.route(
    "/picker/research-market",
    methods=["POST"],
)
def research_market():
    analysis = request.form.get(
        "analysis",
        ""
    )

    identification = request.form.get(
        "identification",
        ""
    )

    if (
        not analysis
        or not identification
    ):
        return page(
            "Missing research data",
            """
            <h2>
                Research data missing
            </h2>

            <p>
                Return to previous step.
            </p>
            """
        ), 400

    market_prompt = f"""
You are performing eBay.com market research
for a vintage collectible item.

VISUAL ANALYSIS:

--------------------------------

{analysis}

--------------------------------

VERIFIED IDENTIFICATION:

--------------------------------

{identification}

--------------------------------

Use VERIFIED IDENTIFICATION as the primary
basis for market searching.

Do not revert to an earlier rejected
identification.

Use web search.

STRICTLY separate:

A)
CONFIRMED SOLD / COMPLETED EBAY LISTINGS

B)
ACTIVE EBAY LISTINGS / ASKING PRICES

Never present an active listing as sold.

For confirmed sold comparables include
when verifiable:

- exact listing title
- eBay item number
- sold price
- currency
- USD equivalent if needed
- sale date
- condition
- working/non-working
- completeness
- important differences from LOT 001

If sold status cannot be independently confirmed,
write:

НЕ ПІДТВЕРДЖЕНО ЯК ПРОДАНИЙ

Prefer:
1. same manufacturer
2. same or near-identical model
3. same case/design
4. same mechanism
5. similar condition

Do not use generic cuckoo clocks as primary
price evidence if closer matches exist.

Adjust for:
- non-working condition
- missing parts
- missing weights
- damage
- restoration needs
- incomplete mechanism
- shipping from Ukraine

Do NOT invent:
- sold status
- item number
- sold price
- date
- model number
- source

Return IN UKRAINIAN.

Use:

LOT 001 — РИНОК EBAY

1. CONFIRMED SOLD EBAY COMPS

2. ACTIVE EBAY LISTINGS

3. ІНШІ КОРИСНІ РИНКОВІ АНАЛОГИ

4. НАЙБЛИЖЧИЙ АНАЛОГ ДО LOT 001

5. КОРЕКЦІЯ ЗА СТАН
ТА НЕКОМПЛЕКТНІСТЬ

6. РЕКОМЕНДОВАНА ЦІНА

Quick sale:
$...

Normal Buy It Now:
$...

Patient seller:
$...

Minimum acceptable offer:
$...

7. РЕКОМЕНДОВАНИЙ EBAY TITLE

Maximum 80 characters.

8. РЕКОМЕНДОВАНА EBAY CATEGORY

9. РЕКОМЕНДОВАНИЙ CONDITION

10. ОСНОВНІ ITEM SPECIFICS

11. РІВЕНЬ ВПЕВНЕНОСТІ 0–100%

12. ЧИ ПОТРІБНА РУЧНА ПЕРЕВІРКА
ПЕРЕД ПУБЛІКАЦІЄЮ

13. ОСНОВНІ ДЖЕРЕЛА

Do NOT publish anything to eBay.

Market research only.
"""

    try:
        market = call_openai(
            {
                "model":
                    "gpt-5.6-terra",

                "tools": [
                    {
                        "type":
                            "web_search"
                    }
                ],

                "reasoning": {
                    "effort": "low"
                },

                "input":
                    market_prompt,

                "max_output_tokens":
                    4000,

                "store": False,
            },
            timeout=270,
        )

    except Exception as exc:
        return page(
            "Market Research Error",
            f"""
            <h2>
                eBay market research error
            </h2>

            <pre style="
                white-space:pre-wrap;
                word-break:break-word;
            ">
{html.escape(str(exc))}
            </pre>

            <p>
                Use Back in the browser.
            </p>
            """
        ), 500

    body = f"""
    <h2>
        LOT 001 —
        Market Research
    </h2>

    <p style="color:green;">
        <b>
            ✓ eBay market research completed
        </b>
    </p>

    <h3>
        Verified Identification
    </h3>

    <div style="
        border:1px solid #ddd;
        padding:14px;
        border-radius:8px;
    ">
        {text_to_html(
            identification
        )}
    </div>     <h3 style="margin-top:25px;">
        eBay Market Research
    </h3>

    <div style="
        border:1px solid #ccc;
        padding:16px;
        border-radius:8px;
        background:#fafafa;
    ">
        {text_to_html(
            market
        )}
    </div>

        <hr style="margin-top:30px;">

    <h3>
        Step 4 — Weight & Shipping
    </h3>

    <p>
        Enter the approximate weight of the item
        WITHOUT packaging.
    </p>

    <form
        action="/picker/shipping"
        method="post"
        style="margin-top:20px;"
    >
        {hidden_field(
            "analysis",
            analysis
        )}

        {hidden_field(
            "identification",
            identification
        )}

        {hidden_field(
            "market",
            market
        )}

        <label>
            <b>
                Item weight without packaging (kg):
            </b>
        </label>

        <br><br>

        <input
            type="number"
            name="item_weight"
            step="0.01"
            min="0.01"
            value="2.4"
            required
            style="
                font-size:20px;
                padding:12px;
                width:180px;
            "
        >

        <br><br>

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;
            "
        >
            Step 4 — Weight & Shipping
        </button>
    </form>
    """

    return page(
        "LOT 001 Market Research",
        body
    )

# ============================================================
# STEP 4 — WEIGHT & SHIPPING
# ============================================================

@app.route(
    "/picker/shipping",
    methods=["POST"],
)
def shipping():
    analysis = request.form.get("analysis", "")
    identification = request.form.get("identification", "")
    market = request.form.get("market", "")
    item_weight_raw = request.form.get("item_weight", "").strip()

    if not analysis or not identification or not market:
        return page(
            "Missing LOT data",
            """
            <h2>LOT data missing</h2>
            <p>Please return to the previous step.</p>
            """,
        ), 400

    try:
        item_weight = float(item_weight_raw.replace(",", "."))
    except (TypeError, ValueError):
        return page(
            "Invalid weight",
            """
            <h2>Invalid weight</h2>
            <p>Please return and enter the item weight in kilograms.</p>
            """,
        ), 400

    if item_weight <= 0:
        return page(
            "Invalid weight",
            """
            <h2>Invalid weight</h2>
            <p>Weight must be greater than 0 kg.</p>
            """,
        ), 400

    # --------------------------------------------------------
    # Estimated packed weight
    #
    # User enters ONLY the unpacked item weight.
    # The system estimates protective packaging separately.
    # This is intentionally conservative for international
    # shipping of vintage / fragile items.
    # --------------------------------------------------------

    if item_weight <= 0.5:
        packaging_allowance = 0.30
    elif item_weight <= 1.0:
        packaging_allowance = 0.45
    elif item_weight <= 2.0:
        packaging_allowance = 0.65
    elif item_weight <= 3.0:
        packaging_allowance = 0.85
    elif item_weight <= 5.0:
        packaging_allowance = 1.10
    elif item_weight <= 10.0:
        packaging_allowance = 1.60
    else:
        packaging_allowance = max(
            2.0,
            item_weight * 0.18,
        )

    estimated_packed_weight = round(
        item_weight + packaging_allowance,
        2,
    )
    base_shipping_usd = 9 + estimated_packed_weight * 9
    shipping_buffer_usd = min(7, max(3, base_shipping_usd * 0.07))
    ebay_shipping_usd = base_shipping_usd + shipping_buffer_usd
    ebay_shipping_usd = int(ebay_shipping_usd) + 0.99
    body = f"""
    <h2>
        LOT 001 — Weight & Shipping
    </h2>

    <p style="color:green;">
        <b>✓ Item weight received</b>
    </p>

    <div style="
        border:1px solid #ddd;
        padding:16px;
        border-radius:8px;
        margin-top:15px;
    ">
        <h3>Weight</h3>

        <p>
            Item weight WITHOUT packaging:
            <b>{item_weight:.2f} kg</b>
        </p>

        <p>
            Estimated packaging allowance:
            <b>{packaging_allowance:.2f} kg</b>
        </p>

        <p>
            Estimated packed shipping weight:
            <b>{estimated_packed_weight:.2f} kg</b>
        </p>

        <p style="font-size:14px;">
            The packed weight is an estimate.
            The item itself was weighed without packaging.
        </p>
    </div>

    <div style="
        border:1px solid #ddd;
        padding:16px;
        border-radius:8px;
        margin-top:20px;
    ">
        <h3>Shipping status</h3>

        <p>
            <b>Origin:</b> Ukraine
        </p>

        <p>
            <b>Primary destination market:</b> USA
        </p>

        <p>
    Item dimensions:
    <b>35 × 29 × 12 cm</b>
</p>

<p>
    Estimated package dimensions:
    <b>45 × 39 × 22 cm</b>
</p>

<p style="font-size:14px;">
    Estimated dimensions include approximately
    5 cm of protective packaging around each side
    of this fragile vintage clock.
</p>


            <p>
    Base shipping: <b>${base_shipping_usd:.2f}</b><br>
    Safety buffer: <b>${shipping_buffer_usd:.2f}</b><br>
    eBay shipping estimate: <b>${ebay_shipping_usd:.2f}</b>
</p>
<p>
    Official Ukrposhta calculator: <b>1851.33 UAH</b>
</p>
    <hr style="margin-top:30px;">

    <h3>LOT 001 status</h3>

    <p>
        ✓ 24 photos<br>
        ✓ Visual AI analysis<br>
        ✓ Verified identification<br>
        ✓ eBay market research<br>
        ✓ Item weight: {item_weight:.2f} kg<br>
        ✓ Estimated packed weight: {estimated_packed_weight:.2f} kg<br>
        ✓ Estimated package dimensions: 45 × 39 × 22 cm<br>
        ✓ Shipping cost Ukraine → USA: 1851.33 UAH<br>
        ⏳ Final eBay listing
    </p>

    <p>
        <b>
            Publication remains blocked until the required
            shipping information is complete.
        </b>
    </p>

    <form
        action="/picker/final-listing"
        method="post"
        style="margin-top:25px;"
    >
        {hidden_field("analysis", analysis)}
        {hidden_field("identification", identification)}
        {hidden_field("market", market)}
        {hidden_field("item_weight", str(item_weight))}
        {hidden_field(
            "estimated_packed_weight",
            str(estimated_packed_weight)
        )}

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;
            "
        
        >
            Create Final eBay Listing
        </button>
    </form>
    """

    return page(
        "LOT 001 Weight & Shipping",
        body,
    )
@app.route("/picker/final-listing", methods=["POST"])
def final_listing():
    analysis = request.form.get("analysis", "")
    identification = request.form.get("identification", "")
    market = request.form.get("market", "")
    item_weight = request.form.get("item_weight", "")
    packed_weight = request.form.get("estimated_packed_weight", "")
    listing_prompt = f"""
Create a final eBay.com listing in English.

Verified identification:
{identification}

Market research:
{market}

Item weight: {item_weight} kg
Packed weight: {packed_weight} kg
Shipping: 1851.33 UAH from Ukraine to USA.

Return plain text only. Do not use Markdown formatting.
1. TITLE — maximum 80 characters
2. PRICE USD
3. CONDITION
4. ITEM SPECIFICS
5. DESCRIPTION
6. SEO KEYWORDS

Important:
- Do not claim an exact model unless verified.
- Do not use "Shalash" as the model unless the evidence confirms it.
- Clearly state that the clock is not working and two weights are missing.
- Do not invent specifications that were not verified.
- PRICE USD must use the Normal / recommended BIN price from Market research exactly.
- Do not recalculate or lower the price in the final listing.
"""
    listing_response = call_openai(
        {
            "model": "gpt-5.6-terra",
            "input": listing_prompt,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 3500,
            "store": False,
        },
        timeout=270,
    )
    return page(
    "LOT 001 Final eBay Listing",
    f"""
    <h2>LOT 001 — Final eBay Listing</h2>
    <pre style="white-space:pre-wrap;">{html.escape(listing_response)}</pre>
    """
)
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
