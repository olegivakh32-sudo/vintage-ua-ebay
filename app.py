import os
import secrets
import requests

from flask import Flask, redirect, request, session

app = Flask(__name__)

app.secret_key = os.environ.get(
    "APP_SECRET",
    "temporary-development-key"
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

# Picker API:
# create session + list selected media
PICKER_API = "https://photospicker.googleapis.com/v1"


def google_headers():
    token = session.get("google_access_token")

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
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
                <button
                    style="font-size:20px;
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
                <button
                    style="font-size:20px;
                    padding:12px 20px;">
                    Connect Google Photos
                </button>
            </a>
        </p>
        """

    return f"""
    <html>

    <head>
        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>Vintage UA eBay</title>
    </head>

    <body
        style="font-family:Arial;
        padding:20px;">

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


# --------------------------------------------------
# GOOGLE OAUTH
# --------------------------------------------------

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

    response = requests.Request(
        "GET",
        GOOGLE_AUTH_URL,
        params=params,
    ).prepare()

    return redirect(response.url)


@app.route("/google/callback")
def google_callback():

    error = request.args.get("error")

    if error:

        return (
            f"Google authorization error: "
            f"{error}",
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
            + token_response.text,
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


# --------------------------------------------------
# GOOGLE PHOTOS PICKER
# --------------------------------------------------

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

        headers=google_headers(),

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
            + response.text,
            500,
        )

    data = response.json()

    picker_session_id = data.get("id")
    picker_uri = data.get("pickerUri")

    if not picker_session_id:

        return (
            "Google did not return "
            "a Picker session ID."
            "<br><br>"
            + str(data),
            500,
        )

    if not picker_uri:

        return (
            "Google did not return "
            "a Picker URI."
            "<br><br>"
            + str(data),
            500,
        )

    session["picker_session_id"] = (
        picker_session_id
    )

    return f"""
    <html>

    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>
            Select LOT photos
        </title>

    </head>

    <body
        style="font-family:Arial;
        padding:20px;">

        <h2>
            LOT photo selection
        </h2>

        <p>
            <b>Step 1.</b>
            Open Google Photos and select
            the 24 photos for this lot.
        </p>

        <p>

            <a
                href="{picker_uri}"
                target="_blank">

                <button
                    style="font-size:20px;
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

                <button
                    style="font-size:20px;
                    padding:12px 20px;">

                    I selected the photos —
                    Continue

                </button>

            </a>

        </p>

    </body>

    </html>
    """


# --------------------------------------------------
# GET SELECTED PHOTOS
# --------------------------------------------------

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

        if response.status_code == 401:

            session.pop(
                "google_access_token",
                None,
            )

            return redirect(
                "/google/login"
            )

        # Google may not have made the
        # selected items available yet.
        if response.status_code in (
            400,
            404,
            409,
        ):

            return """
            <html>

            <head>

                <meta
                    name="viewport"
                    content="width=device-width,
                    initial-scale=1">

            </head>

            <body
                style="font-family:Arial;
                padding:20px;">

                <h2>
                    Waiting for Google Photos
                </h2>

                <p>
                    Google has not made the
                    selected photos available
                    yet.
                </p>

                <p>
                    Wait a few seconds and
                    press the button below.
                </p>

                <p>

                    <a href="/picker/items">

                        <button
                            style="font-size:20px;
                            padding:12px 20px;">

                            Check selected photos

                        </button>

                    </a>

                </p>

            </body>

            </html>
            """

        if not response.ok:

            return (
                "Could not retrieve selected "
                "photos."
                "<br><br>"
                + response.text,
                500,
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

    count = len(items)

    # If API returned successfully but
    # selection has not propagated yet.
    if count == 0:

        return """
        <html>

        <head>

            <meta
                name="viewport"
                content="width=device-width,
                initial-scale=1">

        </head>

        <body
            style="font-family:Arial;
            padding:20px;">

            <h2>
                Waiting for photos
            </h2>

            <p>
                No selected photos have been
                returned yet.
            </p>

            <p>
                Wait a few seconds and
                check again.
            </p>

            <p>

                <a href="/picker/items">

                    <button
                        style="font-size:20px;
                        padding:12px 20px;">

                        Check again

                    </button>

                </a>

            </p>

        </body>

        </html>
        """

    session["selected_photo_count"] = (
        count
    )

    photo_rows = ""

    for number, item in enumerate(
        items,
        start=1,
    ):

        media_file = item.get(
            "mediaFile",
            {}
        )

        filename = media_file.get(
            "filename",
            f"Photo {number}",
        )

        mime_type = media_file.get(
            "mimeType",
            "",
        )

        photo_rows += f"""
        <li style="margin-bottom:8px;">
            {number}. {filename}
            {mime_type}
        </li>
        """

    if count == 24:

        status = (
            "✓ Correct — "
            "24 photos selected."
        )

        status_color = "green"

    else:

        status = (
            f"Attention: expected "
            f"24 photos, but received "
            f"{count}."
        )

        status_color = "red"

    return f"""
    <html>

    <head>

        <meta
            name="viewport"
            content="width=device-width,
            initial-scale=1">

        <title>
            LOT photos
        </title>

    </head>

    <body
        style="font-family:Arial;
        padding:20px;">

        <h2>
            LOT photos received
        </h2>

        <h3>
            Total: {count}
        </h3>

        <p
            style="color:{status_color};">

            <b>
                {status}
            </b>

        </p>

        <ol>
            {photo_rows}
        </ol>

        <p>

            <a href="/">

                <button
                    style="font-size:18px;
                    padding:10px 18px;">

                    Return home

                </button>

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
