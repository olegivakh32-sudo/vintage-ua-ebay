import os
import secrets
import requests

from flask import Flask, redirect, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "temporary-development-key")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

REDIRECT_URI = "https://vintage-ua-ebay-1.onrender.com/google/callback"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

PICKER_SCOPE = (
    "https://www.googleapis.com/auth/"
    "photospicker.mediaitems.readonly"
)

PICKER_API = "https://photoslibrary.googleapis.com/v1"

def google_headers():
    token = session.get("google_access_token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@app.route("/")
def home():
    connected = bool(session.get("google_access_token"))

    if connected:
        google_section = """
        <p style="color:green;"><b>Google Photos connected ✓</b></p>
        <p>
            <a href="/picker/start">
                <button style="font-size:20px;padding:12px 20px;">
                    Select LOT photos
                </button>
            </a>
        </p>
        """
    else:
        google_section = """
        <p>
            <a href="/google/login">
                <button style="font-size:20px;padding:12px 20px;">
                    Connect Google Photos
                </button>
            </a>
        </p>
        """

    return f"""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Vintage UA eBay</title>
    </head>
    <body style="font-family:Arial;padding:20px;">
        <h2>Vintage UA eBay</h2>
        <p>Server is running.</p>
        {google_section}
    </body>
    </html>
    """


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return "GOOGLE_CLIENT_ID is not configured in Render.", 500

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
        return f"Google authorization error: {error}", 400

    received_state = request.args.get("state")
    saved_state = session.pop("google_oauth_state", None)

    if not saved_state or received_state != saved_state:
        return "Invalid OAuth state.", 400

    code = request.args.get("code")

    if not code:
        return "Authorization code was not returned by Google.", 400

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return "Google OAuth credentials are not configured in Render.", 500

    token_response = requests.post(
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

    if not token_response.ok:
        return (
            "Google token exchange failed.<br><br>"
            + token_response.text,
            500,
        )

    token_data = token_response.json()

    session["google_access_token"] = token_data["access_token"]

    if token_data.get("refresh_token"):
        session["google_refresh_token"] = token_data["refresh_token"]

    return redirect("/")


@app.route("/picker/start")
def picker_start():
    if not session.get("google_access_token"):
        return redirect("/google/login")

    response = requests.post(
        f"{PICKER_API}/sessions",
        headers=google_headers(),
        json={},
        timeout=30,
    )

    if response.status_code == 401:
        session.pop("google_access_token", None)
        return redirect("/google/login")

    if not response.ok:
        return (
            "Could not create Google Photos Picker session."
            "<br><br>"
            + response.text,
            500,
        )

    data = response.json()

    session["picker_session_id"] = data["id"]

    picker_uri = data["pickerUri"]

    return f"""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Select LOT photos</title>
    </head>

    <body style="font-family:Arial;padding:20px;">

        <h2>LOT photo selection</h2>

        <p>
            Step 1: Open Google Photos and select the
            <b>24 photos</b> for this lot.
        </p>

        <p>
            <a href="{picker_uri}" target="_blank">
                <button style="font-size:20px;padding:12px 20px;">
                    Open Google Photos
                </button>
            </a>
        </p>

        <p>
            Step 2: After you finish selecting the photos,
            return to this page.
        </p>

        <p>
            <a href="/picker/check">
                <button style="font-size:20px;padding:12px 20px;">
                    I selected the photos — Continue
                </button>
            </a>
        </p>

    </body>
    </html>
    """


@app.route("/picker/check")
def picker_check():
    access_token = session.get("google_access_token")
    picker_session_id = session.get("picker_session_id")

    if not access_token:
        return redirect("/google/login")

    if not picker_session_id:
        return redirect("/picker/start")

    response = requests.get(
        f"{PICKER_API}/sessions/{picker_session_id}",
        headers=google_headers(),
        timeout=30,
    )

    if not response.ok:
        return (
            "Could not check Picker session."
            "<br><br>"
            + response.text,
            500,
        )

    picker_data = response.json()

    if not picker_data.get("mediaItemsSet"):
        return """
        <html>
        <head>
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
        </head>
        <body style="font-family:Arial;padding:20px;">
            <h2>Photos are not confirmed yet</h2>
            <p>
                Finish the selection in Google Photos first.
            </p>
            <p>
                <a href="/picker/check">
                    <button style="font-size:20px;padding:12px 20px;">
                        Check again
                    </button>
                </a>
            </p>
        </body>
        </html>
        """

    return redirect("/picker/items")


@app.route("/picker/items")
def picker_items():
    picker_session_id = session.get("picker_session_id")

    if not session.get("google_access_token"):
        return redirect("/google/login")

    if not picker_session_id:
        return redirect("/picker/start")

    items = []
    page_token = None

    while True:
        params = {
            "sessionId": picker_session_id,
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
            return (
                "Could not retrieve selected photos."
                "<br><br>"
                + response.text,
                500,
            )

        data = response.json()

        items.extend(data.get("mediaItems", []))

        page_token = data.get("nextPageToken")

        if not page_token:
            break

    session["selected_photo_count"] = len(items)

    photo_rows = ""

    for number, item in enumerate(items, start=1):
        media_file = item.get("mediaFile", {})
        filename = media_file.get("filename", f"Photo {number}")

        photo_rows += f"""
            <li style="margin-bottom:8px;">
                {number}. {filename}
            </li>
        """

    count = len(items)

    status = (
        "✓ Correct — 24 photos selected."
        if count == 24
        else f"Attention: expected 24 photos, but received {count}."
    )

    return f"""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>LOT photos</title>
    </head>

    <body style="font-family:Arial;padding:20px;">

        <h2>LOT photos received</h2>

        <h3>Total: {count}</h3>

        <p><b>{status}</b></p>

        <ol>
            {photo_rows}
        </ol>

        <p>
            <a href="/">
                <button style="font-size:18px;padding:10px 18px;">
                    Return home
                </button>
            </a>
        </p>

    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
