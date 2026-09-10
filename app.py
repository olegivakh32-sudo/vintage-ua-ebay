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

# Photos are temporary.
# We intentionally do NOT depend on this cache
# for AI analysis/research results.
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
        <title>{html.escape(title)}</title>
    </head>

    <body style="
        font-family:Arial,sans-serif;
        padding:18px;
        max-width:1000px;
        margin:auto;
        line-height:1.5;">

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
        f'<textarea name="{html.escape(name)}" '
        f'style="display:none;">'
        f'{html.escape(value)}</textarea>'
    )


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

    token = session.get(
        "google_access_token"
    )

    return {
        "Authorization":
            f"Bearer {token}"
    }


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
            "sessionId":
                picker_session_id,
            "pageSize":
                100,
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

        raise RuntimeError(
            "Google baseUrl missing."
        )

    # 1024 px is sufficient for the first pass
    # and keeps API cost/request size under control.
    url = base_url + "=w1024-h1024"

    response = requests.get(
        url,
        headers={
            "Authorization":
                f"Bearer {token}"
        },
        timeout=45,
    )

    if not response.ok:

        raise RuntimeError(
            "Could not download Google photo. "
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
        session.get(
            "google_access_token"
        )
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
        identification research →
        eBay market research →
        shipping →
        final listing
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
            500
        )

    state = secrets.token_urlsafe(32)

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
        params=params
    ).prepare()

    return redirect(
        prepared.url
    )


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

    received_state = request.args.get(
        "state"
    )

    saved_state = session.pop(
        "google_oauth_state",
        None
    )

    if (
        not saved_state
        or received_state != saved_state
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
            400
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
# GOOGLE PICKER START
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
            "Could not create Picker session."
            "<br><pre>"
            + html.escape(
                response.text
            )
            + "</pre>",
            500,
        )

    data = response.json()

    picker_id = data.get("id")
    picker_uri = data.get("pickerUri")

    if not picker_id or not picker_uri:

        return (
            "Google Picker session data missing.",
            500,
        )

    session[
        "picker_session_id"
    ] = picker_id

    body = f"""
    <h2>LOT 001</h2>

    <h3>Step 1</h3>

    <p>
        Select exactly <b>24 photos</b>
        of one item.
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

    <h3>Step 2</h3>

    <p>
        After Google Photos says Done,
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

        body = """
        <h2>No photos yet</h2>

        <p>
            Google returned zero photos.
        </p>

        <p>
            <a href="/picker/items">
                Check again
            </a>
        </p>
        """

        return page(
            "No photos",
            body
        )

    picker_id = session.get(
        "picker_session_id"
    )

    PHOTO_CACHE[
        picker_id
    ] = items

    count = len(items)

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

                {index}. {filename}

            </div>

        </div>
        """

    if count == 24:

        status = """
        <p style="color:green;">
            <b>
                ✓ Correct — 24 photos selected
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
                minmax(135px, 1fr)
            );
        gap:10px;">

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

    if not token or not items:

        return (
            "Photo cache expired",
            404
        )

    if (
        index < 1
        or index > len(items)
    ):

        return (
            "Photo not found",
            404
        )

    item = items[index - 1]

    base_url = item.get(
        "mediaFile",
        {}
    ).get(
        "baseUrl"
    )

    if not base_url:

        return (
            "baseUrl missing",
            404
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
# 24 PHOTO AI ANALYSIS
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

        body = """
        <h2>Photos expired</h2>

        <p>
            Select the 24 photos again.
        </p>

        <p>
            <a href="/picker/start">
                Start again
            </a>
        </p>
        """

        return page(
            "Photos expired",
            body
        ), 400

    if not OPENAI_API_KEY:

        return (
            "OPENAI_API_KEY missing",
            500
        )

    prompt = """
You are analysing ONE vintage collectible lot
for an eBay.com seller.

Study ALL 24 photographs together.

Do not invent information.

Separate what is:
- directly visible
- likely
- unconfirmed

Pay special attention to:
object type,
manufacturer,
brand,
country,
period,
model,
marks,
logos,
stamps,
materials,
mechanism,
completeness,
missing parts,
condition,
damage,
repairs and modifications.

Return IN UKRAINIAN.

Use these sections:

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

Do not research market value yet.
Do not claim to have searched eBay.
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
                        item
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

        body = f"""
        <h2>AI analysis error</h2>

        <pre style="
            white-space:pre-wrap;">
{html.escape(str(exc))}
        </pre>

        <p>
            <a href="/picker/items">
                Back
            </a>
        </p>
        """

        return page(
            "AI Error",
            body
        ), 500

    # IMPORTANT:
    # analysis is carried forward in the HTML form.
    # It is NOT dependent on Render RAM.
    body = f"""
    <h2>
        LOT 001 — AI analysis
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
        background:#fafafa;">

        {text_to_html(analysis)}

    </div>

    <form
        action="/picker/research-identification"
        method="post"
        style="margin-top:25px;">

        {hidden_field("analysis", analysis)}

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;">

            Step 2 — Research identification

        </button>

    </form>
    """

    return page(
        "LOT 001 AI Analysis",
        body
    )


# =========================================================
# RESEARCH STEP 1 — IDENTIFICATION
# =========================================================

@app.route(
    "/picker/research-identification",
    methods=["POST"]
)
def research_identification():

    analysis = request.form.get(
        "analysis",
        ""
    )

    if not analysis:

        return (
            page(
                "Missing analysis",
                """
                <h2>AI analysis missing</h2>
                <p>
                    Run photo analysis again.
                </p>
                """
            ),
            400,
        )

    prompt = f"""
You are identifying a vintage collectible item.

Previous 24-photo visual analysis:

-----------------------------
{analysis}
-----------------------------

Use web search.

Your ONLY task in this step is IDENTIFICATION.

Do not perform broad price research yet.

Search intelligently for:
- exact object type
- manufacturer/factory
- brand
- model or series
- country
- approximate production period
- mechanism
- visible stamps and marks
- distinguishing case/design details

Compare several plausible candidates.

Prefer primary or specialist sources,
catalogues, collector references,
museums, auction archives and exact matches.

Do not invent a manufacturer, model, year,
factory or source.

IMPORTANT:
A symbol or quality mark is not automatically
a manufacturer's mark.

Return IN UKRAINIAN.

Use:

LOT 001 — ІДЕНТИФІКАЦІЯ

1. НАЙІМОВІРНІШЕ ВИЗНАЧЕННЯ
2. ВИРОБНИК / ФАБРИКА
3. МОДЕЛЬ / СЕРІЯ
4. КРАЇНА
5. ПЕРІОД
6. МЕХАНІЗМ / ТИП
7. ДОКАЗИ
8. АЛЬТЕРНАТИВНІ ВАРІАНТИ
9. ЩО НЕ ПІДТВЕРДЖЕНО
10. РІВЕНЬ ВПЕВНЕНОСТІ 0–100%
11. ЯКІ ДОДАТКОВІ ФОТО МОЖУТЬ
    ПІДТВЕРДИТИ ІДЕНТИФІКАЦІЮ
12. ОСНОВНІ ДЖЕРЕЛА

Be concise enough to finish promptly.
"""

    try:

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

                "reasoning": {
                    "effort":
                        "low"
                },

                "input":
                    prompt,

                "max_output_tokens":
                    3000,

                "store":
                    False,
            },
            timeout=270,
        )

    except Exception as exc:

        body = f"""
        <h2>
            Identification research error
        </h2>

        <pre style="
            white-space:pre-wrap;">
{html.escape(str(exc))}
        </pre>

        <p>
            Use Back in the browser.
            Your AI analysis is still
            on the previous page.
        </p>
        """

        return page(
            "Research Error",
            body
        ), 500

    body = f"""
    <h2>
        LOT 001 — Identification
    </h2>

    <p style="color:green;">
        <b>
            ✓ Identification research completed
        </b>
    </p>

    <div style="
        border:1px solid #ccc;
        padding:16px;
        border-radius:8px;
        background:#fafafa;">

        {text_to_html(identification)}

    </div>

    <form
        action="/picker/research-market"
        method="post"
        style="margin-top:25px;">

        {hidden_field("analysis", analysis)}
        {hidden_field("identification", identification)}

        <button
            type="submit"
            style="
                font-size:20px;
                padding:14px 20px;">

            Step 3 — Research eBay market

        </button>

    </form>
    """

    return page(
        "LOT 001 Identification",
        body
    )


# =========================================================
# RESEARCH STEP 2 — EBAY MARKET
# =========================================================

@app.route(
    "/picker/research-market",
    methods=["POST"]
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

    if not analysis or not identification:

        return (
            page(
                "Missing research data",
                """
                <h2>Research data missing</h2>
                <p>
                    Return to the previous step.
                </p>
                """
            ),
            400,
        )

    prompt = f"""
You are performing eBay.com market research
for a vintage collectible item.

VISUAL ANALYSIS:
-----------------------------
{analysis}
-----------------------------

IDENTIFICATION RESEARCH:
-----------------------------
{identification}
-----------------------------

Now focus ONLY on MARKET RESEARCH
and listing recommendation.

Use web search.

Very important:

Separate STRICTLY:

A) confirmed sold/completed eBay listings

B) active eBay listings / asking prices

Never call an active listing a sold listing.

For sold comps, include when verifiable:
- exact listing title
- eBay item number
- sold price
- currency
- date
- condition
- working/non-working
- completeness
- key differences from LOT 001

If sold status cannot be confirmed, write:

НЕ ПІДТВЕРДЖЕНО ЯК ПРОДАНИЙ

Prefer the closest physical/model matches.

Account for:
- non-working condition
- missing parts
- missing weights
- damage
- restoration needs
- shipping from Ukraine

Do not invent:
listing IDs,
sold prices,
dates,
sources,
model numbers.

Return IN UKRAINIAN.

Use:

LOT 001 — РИНОК EBAY

1. CONFIRMED SOLD EBAY COMPS

2. ACTIVE EBAY LISTINGS

3. ІНШІ КОРИСНІ РИНКОВІ АНАЛОГИ

4. КОРЕКЦІЯ ЗА СТАН
ТА НЕКОМПЛЕКТНІСТЬ

5. РЕКОМЕНДОВАНА ЦІНА

Quick sale: $...
Normal Buy It Now: $...
Patient seller: $...
Minimum acceptable offer: $...

6. РЕКОМЕНДОВАНИЙ EBAY TITLE
Maximum 80 characters.

7. РЕКОМЕНДОВАНА EBAY CATEGORY

8. РЕКОМЕНДОВАНИЙ CONDITION

9. ОСНОВНІ ITEM SPECIFICS

10. РІВЕНЬ ВПЕВНЕНОСТІ 0–100%

11. ЧИ ПОТРІБНА РУЧНА ПЕРЕВІРКА
ПЕРЕД ПУБЛІКАЦІЄЮ

12. ОСНОВНІ ДЖЕРЕЛА

Do not publish anything to eBay.

Keep the research focused enough
to complete promptly.
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
                    "effort":
                        "low"
                },

                "input":
                    prompt,

                "max_output_tokens":
                    3500,

                "store":
                    False,
            },
            timeout=270,
        )

    except Exception as exc:

        body = f"""
        <h2>
            eBay market research error
        </h2>

        <pre style="
            white-space:pre-wrap;">
{html.escape(str(exc))}
        </pre>

        <p>
            Use Back in the browser.
            Your identification result
            remains on the previous page.
        </p>
        """

        return page(
            "Market Research Error",
            body
        ), 500

    body = f"""
    <h2>
        LOT 001 — Market Research
    </h2>

    <p style="color:green;">
        <b>
            ✓ eBay market research completed
        </b>
    </p>

    <h3>Identification</h3>

    <div style="
        border:1px solid #ddd;
        padding:14px;
        border-radius:8px;">

        {text_to_html(identification)}

    </div>

    <h3 style="margin-top:25px;">
        eBay market
    </h3>

    <div style="
        border:1px solid #ccc;
        padding:16px;
        border-radius:8px;
        background:#fafafa;">

        {text_to_html(market)}

    </div>

    <hr style="margin-top:30px;">

    <h3>
        Next required stage
    </h3>

    <p>
        Before publication we still need:
    </p>

    <p>
        <b>
        packed weight + package dimensions
        + shipping Ukraine → USA
        </b>
    </p>

    <p>
        The system will NOT publish the lot
        until shipping data is known.
    </p>
    """

    return page(
        "LOT 001 Market Research",
        body
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
