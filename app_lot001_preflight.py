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
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN")
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

    if not response.ok:
        raise RuntimeError(f"eBay refresh token error: {response.text}")

    return response.json()["access_token"]
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
            files={'image': (filename, google_response.content, actual_mime)},
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
                        google_response.content,
                        actual_mime,
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

