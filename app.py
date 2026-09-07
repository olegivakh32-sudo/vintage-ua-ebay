import os
import secrets
import html
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

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

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

# Temporary in-memory cache.
# Good enough for this test.
# It will be lost if Render restarts.
PHOTO_CACHE = {}


def google_headers():
    token = session.get("google_access_token")

    return {
        "Authorization": f"Bearer {token}",
    }


@app.route("/")
def home():

    connected = bool(
        session.get("google_access_token")
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

        <p>Server is running.</p>

        {google_section}

    </body>
    </html>
    """


@app.route("/health")
def health():

    return {
        "status": "ok"
    }


# ==================================================
# GOOGLE OAUTH
# ==================================================

@app.route("/google/login")
def google_login():

    if not GOOGLE_CLIENT_ID:

        return (
            "GOOGLE_CLIENT_ID is not configured "
            "in Render.",
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
            "Authorization code was not "
            "returned by Google.",
            400,
        )

    if (
        not GOOGLE_CLIENT_ID
        or not GOOGLE_CLIENT_SECRET
    ):

        return (
            "Google OAuth credentials are "
            "not configured in Render.",
            500,
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

    # We keep this only for the current prototype.
    # Later we will move refresh tokens to secure
    # server-side storage.
    if token_data.get("refresh_token"):

        session["google_refresh_token"] = (
            token_data["refresh_token"]
        )

    return redirect("/")


# ==================================================
# CREATE PICKER SESSION
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
            "a Picker session ID.",
            500,
        )

    if not picker_uri:

        return (
            "Google did not return "
            "a Picker URI.",
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

        <h2>LOT photo selection</h2>

        <p>
            <b>Step 1.</b>
            Open Google Photos and select
            exactly <b>24 photos</b>
            for this lot.
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
            When Google Photos says
            <b>Done</b>, return here.
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
# GET SELECTED MEDIA ITEMS
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

        return f"""
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

            <h2>
                Photos are not available yet
            </h2>

            <p>
                Wait several seconds and
                try again.
            </p>

            <p>
                <a href="/picker/items">

                    <button style="
                        font-size:20px;
                        padding:12px 20px;">

                        Check again

                    </button>

                </a>
            </p>

            <details>
                <summary>
                    Technical details
                </summary>

                <pre>
{html.escape(error)}
                </pre>
            </details>

        </body>
        </html>
        """

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
                Google returned zero selected
                photos.
            </p>

            <p>
                Wait several seconds and
                press Check again.
            </p>

            <a href="/picker/items">

                <button style="
                    font-size:20px;
                    padding:12px 20px;">

                    Check again

                </button>

            </a>

        </body>
        </html>
        """

    # Save only on server memory.
    # Do NOT put 24 baseUrls into Flask cookie.
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

        prepare_button = """
        <p style="margin-top:25px;">

            <a href="/picker/prepare">

                <button style="
                    font-size:20px;
                    padding:14px 22px;">

                    Prepare LOT 001

                </button>

            </a>

        </p>
        """

    else:

        status = (
            f"Attention: expected "
            f"24 photos, but received "
            f"{count}."
        )

        status_color = "red"

        prepare_button = ""

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

        {prepare_button}

        <p style="margin-top:25px;">

            <a href="/">
                Return home
            </a>

        </p>

    </body>
    </html>
    """


# ==================================================
# THUMBNAIL PROXY
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

    if not picker_session_id:

        return (
            "Picker session missing.",
            404,
        )

    items = PHOTO_CACHE.get(
        picker_session_id
    )

    if not items:

        return (
            "Photo cache expired. "
            "Return to the LOT page.",
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
            "Google did not return "
            "baseUrl for this photo.",
            404,
        )

    # Thumbnail:
    # max width 600px,
    # max height 600px.
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
            "Could not download "
            "Google Photos image."
            "<br><br>"
            + html.escape(
                response.text
            ),
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
# PREPARE LOT
# ==================================================

@app.route("/picker/prepare")
def picker_prepare():

    picker_session_id = session.get(
        "picker_session_id"
    )

    items = PHOTO_CACHE.get(
        picker_session_id,
        [],
    )

    count = len(items)

    if count != 24:

        return (
            f"LOT cannot be prepared. "
            f"Expected 24 photos, "
            f"received {count}.",
            400,
        )

    return """
    <!doctype html>

    <html>
    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>LOT 001 ready</title>

    </head>

    <body style="
        font-family:Arial;
        padding:20px;
        max-width:900px;
        margin:auto;">

        <h2>LOT 001</h2>

        <h3 style="color:green;">
            ✓ 24 photos verified
        </h3>

        <p>
            LOT 001 is ready for the
            next stage.
        </p>

        <p>
            Next: AI analysis,
            identification,
            condition assessment,
            market research and
            eBay listing preparation.
        </p>

        <p>
            <a href="/picker/items">
                Back to photos
            </a>
        </p>

    </body>
    </html>
    """


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
