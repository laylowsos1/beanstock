"""OAuth 2.1 + PKCE support for Beanstock's moomoo read-only adapter.

Implements the pieces broker.moomoo_readonly.MoomooReadOnlyBroker needs
to obtain and refresh an access token, and nothing else:

    register_client()            -- (optional) dynamic client registration
    build_authorization_url()    -- PKCE verifier/challenge + auth URL
    verify_callback_state()      -- constant-time state check
    exchange_authorization_code()-- code -> TokenSet
    refresh_access_token()       -- refresh_token -> new TokenSet
    get_valid_access_token()     -- what the broker actually calls

No token is ever placed in source, written to a plaintext log, or
included in an exception message. Tokens live only inside TokenSet
objects (which redact themselves in repr/str) and inside whatever
auth.token_storage.SecretStore is injected -- never anywhere else.

This module makes no network call except through the injected
HttpTransport (see broker/http_transport.py), so tests never touch a
real socket.

Endpoint paths verified against https://open.moomoo.com/api/overview/getting-started
on an official-docs verification pass (see broker/MOOMOO_API_CONTRACT.md
for the full note): register at POST /oauth2/register, authorize at
GET /oauth2/authorize/confirm, and both code exchange and refresh at
POST /oauth2/token, all against host https://webapi.moomoo.com. These
are OAuthConfig's actual defaults below, not placeholders.

Two things that documentation page does NOT specify, and which this
module therefore refuses to guess at:

- The real `scope` string value(s) moomoo issues. Only the field name
  `scope` is confirmed. OAuthConfig.scope has no default -- callers
  must supply the value moomoo's docs/dashboard give them for their
  own registered application.
- `client_secret`: the documented /oauth2/register response never
  includes one (token_endpoint_auth_method="none" -- a public,
  PKCE-only client). It remains an optional parameter here purely for
  forward-compatibility; the verified flow never produces or requires it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
import base64
import hashlib
import json
import secrets

from broker.http_transport import HttpTransport, TransportError, TransportTimeout
from auth.token_storage import SecretStore

DEFAULT_PKCE_METHOD = "S256"

# Verified against https://open.moomoo.com/api/overview/getting-started.
DEFAULT_REGISTER_PATH = "/oauth2/register"
DEFAULT_AUTHORIZE_PATH = "/oauth2/authorize/confirm"
DEFAULT_TOKEN_PATH = "/oauth2/token"

# Verified scope vocabulary (https://open.moomoo.com/api/overview/getting-started):
# quote:read, quote:write, trade:read, trade:write, accid:* (narrows to
# accid:{account_id} once a token is issued).
#
# READ_ONLY_SCOPE deliberately requests only quote:read and trade:read --
# least privilege for this stage. accid:* is NOT requested: nothing this
# project has fetched from official docs states that the sim-trade
# endpoints (broker/MOOMOO_API_CONTRACT.md) require an accid scope at
# all -- the only accid evidence found is a generic scope-vocabulary
# example showing it can appear in a token response, not a documented
# per-endpoint requirement. Do not add accid:* or accid:<id> back
# without both (a) a specific official-docs citation establishing that
# a sim-trade (or quote) endpoint requires it, or a real API error from
# moomoo saying so, and (b) explicit owner approval -- never guess this
# scope wider than necessary, and never let it silently expand to cover
# a live/real account.
READ_ONLY_SCOPE = "quote:read trade:read"

_TOKEN_STORAGE_KEY = "moomoo_oauth_token_set"
_REGISTRATION_STORAGE_KEY = "moomoo_oauth_client_registration"
_TOKEN_EXPIRY_SKEW_SECONDS = 30


# ---------------------------------------------------------------------
# Errors -- messages are always built from status codes / known-safe
# JSON fields, never from a raw response body or a token value.
# ---------------------------------------------------------------------


class MoomooAuthError(Exception):
    """Base for every auth failure in this module. Fail closed: callers
    should treat any of these as "no usable access token" rather than
    guessing at a fallback.
    """


class AuthenticationError(MoomooAuthError):
    """No usable access token is available and none could be obtained."""


class CallbackStateError(MoomooAuthError):
    """The OAuth callback's `state` did not match what was issued."""


class ClientRegistrationError(MoomooAuthError):
    """Dynamic client registration failed."""


class AuthorizationCodeExchangeError(MoomooAuthError):
    """Exchanging an authorization code for tokens failed."""


class TokenRefreshError(MoomooAuthError):
    """Refreshing the access token failed."""


def _sanitize_error_detail(response_body: str) -> str:
    """Extract only the standard OAuth error fields (error,
    error_description) from a JSON error body, if present. Never
    returns the raw body verbatim -- a misbehaving or misconfigured
    server could in principle echo request data (including a token)
    back in an error body, so nothing beyond these two named fields is
    ever surfaced.
    """
    try:
        parsed = json.loads(response_body)
    except (ValueError, TypeError):
        return "non-JSON error response"
    if not isinstance(parsed, dict):
        return "unexpected error response shape"
    error = parsed.get("error")
    description = parsed.get("error_description")
    parts = [str(p) for p in (error, description) if p]
    return "; ".join(parts) if parts else "no error detail provided"


# ---------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = DEFAULT_PKCE_METHOD

    def __repr__(self) -> str:
        return f"PkcePair(method={self.method!r}, verifier='***redacted***', challenge='***redacted***')"

    __str__ = __repr__


def generate_pkce_pair() -> PkcePair:
    """RFC 7636 S256 PKCE pair. The verifier is a high-entropy random
    string (never derived from anything guessable); the challenge is the
    base64url-encoded (no padding) SHA-256 digest of the verifier.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(verifier=verifier, challenge=challenge, method=DEFAULT_PKCE_METHOD)


def generate_state() -> str:
    """High-entropy CSRF state value for the authorization request."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: Optional[str]
    token_type: str
    expires_at: datetime  # always timezone-aware, UTC
    scope: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"TokenSet(token_type={self.token_type!r}, scope={self.scope!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, "
            f"access_token='***redacted***', "
            f"refresh_token={'***redacted***' if self.refresh_token else None})"
        )

    __str__ = __repr__

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        return moment >= (self.expires_at - timedelta(seconds=_TOKEN_EXPIRY_SKEW_SECONDS))

    def to_storage_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat(),
            "scope": self.scope,
        }

    @classmethod
    def from_storage_dict(cls, raw: dict) -> "TokenSet":
        expires_at = datetime.fromisoformat(raw["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return cls(
            access_token=raw["access_token"],
            refresh_token=raw.get("refresh_token"),
            token_type=raw.get("token_type", "Bearer"),
            expires_at=expires_at,
            scope=raw.get("scope"),
        )

    @classmethod
    def from_token_response(cls, payload: dict, now: Optional[datetime] = None) -> "TokenSet":
        moment = now or datetime.now(timezone.utc)
        access_token = payload.get("access_token")
        if not access_token:
            raise AuthorizationCodeExchangeError("Token response is missing access_token.")
        expires_in = payload.get("expires_in", 3600)
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            expires_in = 3600.0
        return cls(
            access_token=access_token,
            refresh_token=payload.get("refresh_token"),
            token_type=payload.get("token_type", "Bearer"),
            expires_at=moment + timedelta(seconds=expires_in),
            scope=payload.get("scope"),
        )


class TokenStorage:
    """Thin wrapper turning a SecretStore into TokenSet load/save/clear.
    A malformed or unreadable stored value is treated the same as "no
    token stored" (forces a fresh auth/refresh) rather than raising --
    corrupted local secret storage should never crash the caller, it
    should just mean re-authentication is required.
    """

    def __init__(self, secret_store: SecretStore, key: str = _TOKEN_STORAGE_KEY):
        self._store = secret_store
        self._key = key

    def load(self) -> Optional[TokenSet]:
        raw = self._store.get(self._key)
        if not raw:
            return None
        try:
            return TokenSet.from_storage_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            return None

    def save(self, token_set: TokenSet) -> None:
        self._store.set(self._key, json.dumps(token_set.to_storage_dict()))

    def clear(self) -> None:
        self._store.delete(self._key)


# ---------------------------------------------------------------------
# Dynamic client registration (RFC 7591-shaped; confirm moomoo actually
# supports this before relying on it -- many OAuth providers instead
# issue a client_id/secret through a developer portal, in which case
# skip register_client() and construct OAuthConfig with those values
# directly).
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClientRegistration:
    client_id: str
    client_secret: Optional[str]
    registration_access_token: Optional[str]
    registration_client_uri: Optional[str]
    pkce_required: Optional[bool] = None

    def __repr__(self) -> str:
        return (
            f"ClientRegistration(client_id={self.client_id!r}, "
            f"client_secret={'***redacted***' if self.client_secret else None}, "
            f"registration_access_token={'***redacted***' if self.registration_access_token else None}, "
            f"registration_client_uri={self.registration_client_uri!r}, "
            f"pkce_required={self.pkce_required!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class OAuthConfig:
    """redirect_uri and scope have no defaults on purpose: redirect_uri
    is inherently caller-specific, and the real `scope` string value(s)
    moomoo issues are not given anywhere in the fetched official docs
    (only the field name is confirmed) -- see broker/MOOMOO_API_CONTRACT.md.
    The other fields default to the host/paths verified against
    https://open.moomoo.com/api/overview/getting-started.

    base_host matters here specifically because build_authorization_url()
    produces a URL meant to be opened directly in the user's browser --
    unlike register/token, which go through an injected HttpTransport
    that already knows its own base host, a bare browser URL needs the
    scheme+host spelled out or it isn't a URL at all.
    """

    redirect_uri: str
    scope: str
    base_host: str = "https://webapi.moomoo.com"
    authorize_endpoint: str = DEFAULT_AUTHORIZE_PATH
    token_endpoint: str = DEFAULT_TOKEN_PATH
    registration_endpoint: Optional[str] = DEFAULT_REGISTER_PATH


class MoomooOAuthClient:
    def __init__(
        self,
        http_transport: HttpTransport,
        config: OAuthConfig,
        token_storage: TokenStorage,
        registration_store: Optional[SecretStore] = None,
    ):
        self._transport = http_transport
        self._config = config
        self._token_storage = token_storage
        self._registration_store = registration_store

    # -- dynamic client registration ----------------------------------

    def register_client(self, client_name: str, redirect_uris: list) -> ClientRegistration:
        if not self._config.registration_endpoint:
            raise ClientRegistrationError("No registration_endpoint configured.")
        try:
            response = self._transport.post(
                self._config.registration_endpoint,
                json_body={
                    "client_name": client_name,
                    "redirect_uris": redirect_uris,
                    "token_endpoint_auth_method": "none",  # public client, PKCE-only
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
        except (TransportError, TransportTimeout) as exc:
            raise ClientRegistrationError(f"Registration request failed ({type(exc).__name__}).") from None

        if not (200 <= response.status_code < 300):
            raise ClientRegistrationError(
                f"Registration rejected (HTTP {response.status_code}): "
                f"{_sanitize_error_detail(response.body)}"
            )
        try:
            payload = response.json()
            client_id = payload["client_id"]
        except (ValueError, KeyError, TypeError):
            raise ClientRegistrationError("Malformed client registration response.") from None

        registration = ClientRegistration(
            client_id=client_id,
            client_secret=payload.get("client_secret"),
            registration_access_token=payload.get("registration_access_token"),
            registration_client_uri=payload.get("registration_client_uri"),
            pkce_required=payload.get("pkce_required"),
        )
        if self._registration_store is not None:
            self._registration_store.set(
                _REGISTRATION_STORAGE_KEY,
                json.dumps(
                    {
                        "client_id": registration.client_id,
                        "client_secret": registration.client_secret,
                        "registration_access_token": registration.registration_access_token,
                        "registration_client_uri": registration.registration_client_uri,
                    }
                ),
            )
        return registration

    def get_stored_registration(self) -> Optional[ClientRegistration]:
        """Read back whatever register_client() last persisted, without
        making a network call. Returns None if nothing is stored or the
        registration_store is malformed/absent -- callers should treat
        that the same as "not registered yet" and call register_client().
        """
        if self._registration_store is None:
            return None
        raw = self._registration_store.get(_REGISTRATION_STORAGE_KEY)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return ClientRegistration(
                client_id=payload["client_id"],
                client_secret=payload.get("client_secret"),
                registration_access_token=payload.get("registration_access_token"),
                registration_client_uri=payload.get("registration_client_uri"),
                pkce_required=payload.get("pkce_required"),
            )
        except (ValueError, KeyError, TypeError):
            return None

    # -- authorization-code + PKCE flow --------------------------------

    def build_authorization_url(self, client_id: str) -> tuple:
        """Returns (url, state, code_verifier). The caller must hold
        state and code_verifier itself across the redirect (e.g. in
        session state) -- this client is stateless between calls.
        """
        pkce = generate_pkce_pair()
        state = generate_state()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": self._config.scope,
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self._config.base_host}{self._config.authorize_endpoint}?{query}"
        return url, state, pkce.verifier

    def verify_callback_state(self, expected_state: str, received_state: str) -> None:
        if not secrets.compare_digest(str(expected_state), str(received_state or "")):
            raise CallbackStateError("OAuth callback state does not match; possible CSRF, rejecting.")

    def exchange_authorization_code(
        self,
        code: str,
        code_verifier: str,
        client_id: str,
        client_secret: Optional[str] = None,
    ) -> TokenSet:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._config.redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        if client_secret:
            form["client_secret"] = client_secret
        try:
            response = self._transport.post(self._config.token_endpoint, form=form)
        except (TransportError, TransportTimeout) as exc:
            raise AuthorizationCodeExchangeError(
                f"Token exchange request failed ({type(exc).__name__})."
            ) from None

        if not (200 <= response.status_code < 300):
            raise AuthorizationCodeExchangeError(
                f"Token exchange rejected (HTTP {response.status_code}): "
                f"{_sanitize_error_detail(response.body)}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise AuthorizationCodeExchangeError("Malformed token exchange response.") from None

        token_set = TokenSet.from_token_response(payload)
        self._token_storage.save(token_set)
        return token_set

    # -- refresh --------------------------------------------------------

    def refresh_access_token(self, client_id: str, client_secret: Optional[str] = None) -> TokenSet:
        current = self._token_storage.load()
        if current is None or not current.refresh_token:
            raise TokenRefreshError("No refresh token available; a new authorization flow is required.")

        form = {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            form["client_secret"] = client_secret
        try:
            response = self._transport.post(self._config.token_endpoint, form=form)
        except (TransportError, TransportTimeout) as exc:
            raise TokenRefreshError(f"Token refresh request failed ({type(exc).__name__}).") from None

        if not (200 <= response.status_code < 300):
            raise TokenRefreshError(
                f"Token refresh rejected (HTTP {response.status_code}): "
                f"{_sanitize_error_detail(response.body)}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise TokenRefreshError("Malformed token refresh response.") from None

        try:
            new_token_set = TokenSet.from_token_response(payload)
        except AuthorizationCodeExchangeError as exc:
            raise TokenRefreshError(str(exc)) from None

        # Some providers omit refresh_token on refresh, meaning "reuse
        # the existing one" -- never silently drop the ability to
        # refresh again.
        if new_token_set.refresh_token is None and current.refresh_token is not None:
            new_token_set = TokenSet(
                access_token=new_token_set.access_token,
                refresh_token=current.refresh_token,
                token_type=new_token_set.token_type,
                expires_at=new_token_set.expires_at,
                scope=new_token_set.scope,
            )
        self._token_storage.save(new_token_set)
        return new_token_set

    def get_valid_access_token(self, client_id: str, client_secret: Optional[str] = None) -> str:
        """What MoomooReadOnlyBroker actually calls before every request.
        Fails closed with AuthenticationError -- never returns a stale
        or empty token, and never silently proceeds without one.
        """
        current = self._token_storage.load()
        if current is None:
            raise AuthenticationError("No stored OAuth session; run the authorization flow first.")
        if not current.is_expired():
            return current.access_token
        try:
            refreshed = self.refresh_access_token(client_id, client_secret)
        except TokenRefreshError as exc:
            raise AuthenticationError(f"Access token expired and refresh failed: {exc}") from None
        return refreshed.access_token
