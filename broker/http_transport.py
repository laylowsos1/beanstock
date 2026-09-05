"""Injectable HTTP transport for Beanstock's moomoo adapters.

Kept as its own tiny module so both broker/moomoo_readonly.py and
auth/moomoo_oauth.py can share one swappable HTTP boundary instead of
each rolling their own. Production code talks to the network only
through UrllibHttpTransport (Python stdlib only -- no extra dependency
required just to read quotes); every test injects a fake transport that
returns canned responses, so no test in this project ever opens a
socket.

This module makes no assumption about what any endpoint returns -- it
only moves bytes. Response interpretation (JSON shape, field names,
error semantics) belongs in the caller (broker/moomoo_readonly.py,
auth/moomoo_oauth.py), not here.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import json
import urllib.error
import urllib.parse
import urllib.request


class TransportError(Exception):
    """Base for transport-level failures. Never carries request/response
    bodies verbatim -- only what's needed to diagnose connectivity,
    never anything that could contain a token or other secret.
    """


class TransportTimeout(TransportError):
    """The request did not complete within the given timeout."""


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str

    def json(self):
        """Parse the body as JSON. Raises json.JSONDecodeError (a
        ValueError) on malformed JSON -- callers are expected to catch
        that and fail closed with a sanitized error.
        """
        return json.loads(self.body)


class HttpTransport(ABC):
    """Minimal GET/POST transport contract. No method here performs
    retries, auth, or response interpretation -- callers own that.
    """

    @abstractmethod
    def get(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        ...

    @abstractmethod
    def post(
        self,
        path: str,
        *,
        form: Optional[dict] = None,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        ...


class UrllibHttpTransport(HttpTransport):
    """Production transport. Deliberately stdlib-only (urllib) so this
    read-only adapter introduces no new third-party dependency; a
    `requests`-based transport can be swapped in later by implementing
    the same HttpTransport contract -- nothing above this class would
    need to change.

    base_host defaults conceptually to https://webapi.moomoo.com per
    moomoo's OpenAPI docs, but is always configurable and never assumed
    to be a live-trading host by anything in this module -- this class
    has no notion of "live" vs "simulated" at all; that distinction is
    enforced entirely in broker/moomoo_readonly.py.
    """

    def __init__(self, base_host: str = "https://webapi.moomoo.com"):
        if not base_host:
            raise ValueError("base_host must be a non-empty string")
        self._base_host = base_host.rstrip("/")

    def get(self, path, *, params=None, headers=None, timeout=10.0):
        url = self._base_host + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers or {}, method="GET")
        return self._send(request, timeout)

    def post(self, path, *, form=None, json_body=None, headers=None, timeout=10.0):
        url = self._base_host + path
        headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = b""
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        return self._send(request, timeout)

    def _send(self, request: "urllib.request.Request", timeout: float) -> HttpResponse:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return HttpResponse(
                    status_code=resp.status,
                    body=resp.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            # A non-2xx status that urllib treats as an exception is
            # still a real HTTP response -- surface it as one rather
            # than as a transport failure, so status-code handling
            # stays centralized in the caller.
            return HttpResponse(
                status_code=exc.code,
                body=exc.read().decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            raise TransportTimeout("Request to moomoo host timed out.") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TransportTimeout("Request to moomoo host timed out.") from None
            # Never include exc's full text -- on some platforms it can
            # echo back parts of the request. Only the failure class.
            raise TransportError(
                f"Network error contacting moomoo host ({type(exc.reason).__name__})."
            ) from None
