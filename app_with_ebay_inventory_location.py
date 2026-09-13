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
