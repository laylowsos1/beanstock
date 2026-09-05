"""One-time, interactive moomoo OAuth 2.1 + PKCE read-only login.

Run this yourself, in your own terminal, so the browser step is really
you logging into your real moomoo account:

    python -m auth.moomoo_login

What it does, in order (A-G, matching the requested workflow):

  A. Reuses a previously registered OAuth public client if one is
     already stored; otherwise registers a new one (POST /oauth2/register).
  B. Requests exactly auth.moomoo_oauth.READ_ONLY_SCOPE
     ("quote:read trade:read") -- never quote:write, trade:write, or
     accid:* (no official-docs citation establishes that the sim-trade
     endpoints require an account-scoped grant; see the comment above
     READ_ONLY_SCOPE in auth/moomoo_oauth.py before ever widening this).
  C. Opens the authorization URL in your default browser (and prints it,
     in case the automatic open doesn't work in this environment) --
     this is the step where YOU log into your real moomoo account.
  D. Runs a local, loopback-only HTTP listener to catch the redirect,
     and validates the returned `state` against the one this process
     generated before ever using the returned `code`.
  E. Exchanges the authorization code for tokens.
  F. Stores the client registration and the tokens in the real Windows
     Credential Manager (auth.token_storage.WindowsCredentialSecretStore)
     -- never in a source file, a JSON file, or Git.
  G. Never prints an access_token, refresh_token, or
     registration_access_token value, and never logs an Authorization
     header. The local callback server also disables its default
     request logging, because that would otherwise print the
     authorization code (in the callback URL's query string) to stdout.

This script makes real network calls to https://webapi.moomoo.com (or
whatever --base-host is given) and opens a real browser window. It
places no order, modifies no order, and cancels no order -- it only
ever calls the OAuth endpoints in broker/MOOMOO_API_CONTRACT.md.
"""

import argparse
import http.server
import re
import sys
import threading
import urllib.parse
import webbrowser
from typing import Optional

from auth.moomoo_oauth import (
    READ_ONLY_SCOPE,
    AuthenticationError,
    AuthorizationCodeExchangeError,
    CallbackStateError,
    ClientRegistrationError,
    MoomooOAuthClient,
    OAuthConfig,
    TokenStorage,
)
from auth.token_storage import SecretStore, WindowsCredentialSecretStore
from broker.http_transport import UrllibHttpTransport

DEFAULT_REDIRECT_PORT = 8765
CALLBACK_TIMEOUT_SECONDS = 300.0

_ACCID_PATTERN = re.compile(r"accid:\d+")


def _sanitize_scope_for_display(scope: Optional[str]) -> Optional[str]:
    """Scope strings are not secret, but a granted scope like
    'quote:read trade:read accid:123456' does embed a real account id
    (see broker/MOOMOO_API_CONTRACT.md) -- mask it before printing.
    """
    if scope is None:
        return None
    return _ACCID_PATTERN.sub("accid:***", scope)


class _CallbackResult:
    def __init__(self):
        self.code: Optional[str] = None
        self.state: Optional[str] = None
        self.error: Optional[str] = None
        self.event = threading.Event()


def _make_handler(result: "_CallbackResult"):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            result.code = query.get("code", [None])[0]
            result.state = query.get("state", [None])[0]
            result.error = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><p>Beanstock: authorization received. "
                b"You may close this window.</p></body></html>"
            )
            result.event.set()

        def log_message(self, format, *args):
            # The default implementation prints the request line -- which
            # includes the authorization code in the query string -- to
            # stdout. Never do that.
            return

    return Handler


def _wait_for_callback(port: int, timeout_seconds: float) -> "_CallbackResult":
    result = _CallbackResult()
    server = http.server.HTTPServer(("127.0.0.1", port), _make_handler(result))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        received = result.event.wait(timeout=timeout_seconds)
        if not received:
            raise TimeoutError(f"No OAuth callback received within {timeout_seconds:.0f}s.")
        return result
    finally:
        server.shutdown()
        thread.join(timeout=5)


def run_login(
    *,
    base_host: str = "https://webapi.moomoo.com",
    redirect_port: int = DEFAULT_REDIRECT_PORT,
    client_id_override: Optional[str] = None,
    transport=None,
    token_secret_store: Optional[SecretStore] = None,
    registration_secret_store: Optional[SecretStore] = None,
    callback_timeout_seconds: float = CALLBACK_TIMEOUT_SECONDS,
) -> None:
    transport = transport or UrllibHttpTransport(base_host)
    token_storage = TokenStorage(token_secret_store or WindowsCredentialSecretStore())
    registration_store = registration_secret_store or WindowsCredentialSecretStore()

    redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"
    config = OAuthConfig(redirect_uri=redirect_uri, scope=READ_ONLY_SCOPE, base_host=base_host)
    client = MoomooOAuthClient(transport, config, token_storage, registration_store=registration_store)

    client_id = client_id_override
    if client_id is None:
        stored = client.get_stored_registration()
        client_id = stored.client_id if stored is not None else None

    if client_id:
        print(f"[A] Reusing previously registered OAuth client (client_id={client_id}).")
    else:
        print("[A] No existing OAuth client registration found -- registering a new public client...")
        try:
            registration = client.register_client(
                client_name="Beanstock Read-Only Adapter", redirect_uris=[redirect_uri]
            )
        except ClientRegistrationError as exc:
            print(f"[A] FAILED: client registration failed: {exc}")
            raise SystemExit(1)
        client_id = registration.client_id
        print(f"[A] Registered new OAuth client (client_id={client_id}). Stored securely, not in Git.")

    print(f"[B] Requesting read-only scope: {READ_ONLY_SCOPE!r}")
    url, state, code_verifier = client.build_authorization_url(client_id)

    print("[C] Opening the authorization URL in your default browser.")
    print(f"    If it doesn't open automatically, open this URL yourself:\n    {url}")
    print("    Log in with your REAL moomoo account and approve the read-only consent screen.")
    webbrowser.open(url)

    print(f"[D] Waiting up to {callback_timeout_seconds:.0f}s for the redirect callback on {redirect_uri} ...")
    try:
        result = _wait_for_callback(redirect_port, callback_timeout_seconds)
    except TimeoutError as exc:
        print(f"[D] FAILED: {exc}")
        raise SystemExit(1)

    if result.error:
        print(f"[D] FAILED: moomoo returned an authorization error: {result.error!r}")
        raise SystemExit(1)
    if not result.code or not result.state:
        print("[D] FAILED: callback did not include both code and state.")
        raise SystemExit(1)

    try:
        client.verify_callback_state(state, result.state)
    except CallbackStateError as exc:
        print(f"[D] FAILED: {exc}")
        raise SystemExit(1)
    print("[D] Callback received; state verified.")

    print("[E] Exchanging authorization code for tokens...")
    try:
        token_set = client.exchange_authorization_code(result.code, code_verifier, client_id)
    except AuthorizationCodeExchangeError as exc:
        print(f"[E] FAILED: {exc}")
        raise SystemExit(1)

    print("[F] Tokens stored securely via Windows Credential Manager (never in Git, never in a source file).")
    print(f"    Token type: {token_set.token_type}")
    print(f"    Granted scope: {_sanitize_scope_for_display(token_set.scope)}")
    print(f"    Expires at: {token_set.expires_at.isoformat()}")
    print("[G] Token values are never printed. Login complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-host", default="https://webapi.moomoo.com")
    parser.add_argument("--redirect-port", type=int, default=DEFAULT_REDIRECT_PORT)
    parser.add_argument(
        "--client-id", default=None, help="Reuse an already-registered client_id instead of registering a new one."
    )
    args = parser.parse_args()
    run_login(base_host=args.base_host, redirect_port=args.redirect_port, client_id_override=args.client_id)
