import os
import secrets

import requests
from flask import Flask, redirect, request, session

app = Flask(__name__)

# Секрет для сесії беремо тільки з Environment Variables Render
app.secret_key = os.environ.get("APP_SECRET", "temporary-development-key")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

REDIRECT_URI = "https://vintage-ua-ebay-1.onrender.com/google/callback"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

PHOTO_PICKER_SCOPE = (
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
)


@app.route("/")
def home():
    return """
    <h2>Vintage UA eBay</h2>
    <p>Server is running.</p>
    <p><a href="/google/login">Connect Google Photos</a></p>
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
        "scope": PHOTO_PICKER_SCOPE,
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
            400,
        )

    token_data = token_response.json()

    # Поки що перевіряємо лише успішну авторизацію.
    # Сам токен навмисно НЕ показуємо на екрані.
    if "access_token" not in token_data:
        return "Google did not return an access token.", 400

    return """
    <h2>Google Photos connected successfully!</h2>
    <p>Authorization completed.</p>
    <p>Next step: connect the Google Photos Picker.</p>
    <p><a href="/">Return to Vintage UA eBay</a></p>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
