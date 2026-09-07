import base64
import hashlib
import os
import secrets
import time
import requests

from urllib.parse import parse_qs, urlencode, urlparse

from tiddl.core.api import TidalAPI, TidalClient
from tiddl.core.api.exceptions import ApiError
from tiddl.cli.utils.auth import AuthData, save_auth_data, load_auth_data

PKCE_CLIENT_ID = "6BDSRdpK9hqEBTgU"
PKCE_CLIENT_SECRET = "xeuPmY7nbpZ9IIbLAcQ93shka1VNheUAqN6IcszjTG8="
PKCE_REDIRECT_URI = "https://tidal.com/android/login/auth"

PKCE_STATE = {}

def start_pkce_auth() -> str:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    uniq = secrets.token_hex(8)

    PKCE_STATE["verifier"] = verifier
    PKCE_STATE["uniq"] = uniq

    params = {
        "response_type": "code",
        "redirect_uri": PKCE_REDIRECT_URI,
        "client_id": PKCE_CLIENT_ID,
        "lang": "en",
        "appMode": "android",
        "client_unique_key": uniq,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "restrict_signup": "true",
        "scope": "r_usr w_usr",
    }
    return "https://login.tidal.com/authorize?" + urlencode(params)


def finish_pkce_auth(redirect_url_or_code: str) -> dict:
    url_str = redirect_url_or_code.strip()

    if "code=" in url_str:
        qs = parse_qs(urlparse(url_str).query)
        code = qs.get("code", [""])[0]
    else:
        code = url_str

    if not code:
        return {"ok": False, "error": "No authorization code found."}

    verifier = PKCE_STATE.get("verifier")
    uniq = PKCE_STATE.get("uniq")

    if not verifier:
        return {"ok": False, "error": "Session expired. Run /login again."}

    res = requests.post(
        "https://auth.tidal.com/v1/oauth2/token",
        data={
            "client_id": PKCE_CLIENT_ID,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": PKCE_REDIRECT_URI,
            "scope": "r_usr w_usr",
            "code_verifier": verifier,
            "client_unique_key": uniq,
        },
        auth=(PKCE_CLIENT_ID, PKCE_CLIENT_SECRET),
        timeout=20,
    )

    if res.status_code != 200:
        try:
            msg = res.json().get("error_description", res.text[:120])
        except Exception:
            msg = res.text[:120]
        return {"ok": False, "error": f"HTTP {res.status_code}: {msg}"}

    data = res.json()
    user = data.get("user", {})

    save_auth_data(
        AuthData(
            token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_in"] + int(time.time()),
            user_id=str(data.get("user_id")),
            country_code=user.get("countryCode"),
        )
    )

    PKCE_STATE.clear()
    return {"ok": True, "user_id": data.get("user_id"), "country": user.get("countryCode")}


def check_pkce_token(tidal: TidalAPI):
    auth_data = load_auth_data()
    try:
        session = tidal.get_session()
    except ApiError as e:
        # log.warn(e.user_message)
        # log.info("Refreshing token...")
        result = refresh_pkce_token(auth_data.refresh_token)
        auth_data = load_auth_data()
        # log.success("Token refreshed")
        tidal.client = TidalClient(
            token=auth_data.token,
            cache_name="./tidal_cache",
        )

def refresh_pkce_token(refresh_token: str) -> dict:
    res = requests.post(
        "https://auth.tidal.com/v1/oauth2/token",
        data={
            "client_id": PKCE_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(PKCE_CLIENT_ID, PKCE_CLIENT_SECRET),
        timeout=20,
    )

    if res.status_code != 200:
        try:
            msg = res.json().get("error_description", res.text[:120])
        except Exception:
            msg = res.text[:120]
        return {"ok": False, "error": f"HTTP {res.status_code}: {msg}"}

    data = res.json()
    user = data.get("user", {})

    new_refresh_token = data.get("refresh_token", refresh_token)

    save_auth_data(
        AuthData(
            token=data["access_token"],
            refresh_token=new_refresh_token,
            expires_at=data["expires_in"] + int(time.time()),
            user_id=str(data.get("user_id", "")),
            country_code=user.get("countryCode"),
        )
    )

    return {"ok": True, "access_token": data["access_token"]}
