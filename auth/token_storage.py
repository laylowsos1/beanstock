"""Injectable secret storage for Beanstock's moomoo OAuth client.

Nothing in this module ever writes a secret to Git, a log line, or an
exception message. It defines the storage *contract* plus:

- InMemorySecretStore: process-memory only, for tests and short-lived
  interactive flows. Never persisted; gone when the process exits.
- WindowsCredentialSecretStore: a real Windows Credential Manager-backed
  store, implemented directly against the Win32 Credential Manager API
  (advapi32.dll: CredWriteW/CredReadW/CredDeleteW/CredFree) via ctypes.
  No third-party dependency (no `keyring`, no `pywin32`) is required --
  this project has no dependency file today, and the stdlib is enough.
  Secrets are encrypted at rest by Windows itself (DPAPI, tied to the
  logged-in Windows user) -- this module never implements its own
  encryption and never falls back to plaintext if the Windows call
  fails; it raises instead.

auth.moomoo_oauth builds TokenStorage on top of whichever SecretStore is
injected -- the OAuth client itself never knows or cares whether secrets
live in memory or in the OS vault.
"""

from abc import ABC, abstractmethod
from typing import Optional
import ctypes
import sys

if sys.platform == "win32":
    from ctypes import wintypes


class SecretStore(ABC):
    """A minimal get/set/delete-by-key secret store. Values are opaque
    strings (callers serialize/deserialize their own structures) so this
    contract stays storage-agnostic.
    """

    @abstractmethod
    def get(self, key: str) -> Optional[str]:
        ...

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...


class InMemorySecretStore(SecretStore):
    """Process-memory-only secret store. Suitable for tests and for a
    single interactive session; NOT durable -- restart the process and
    every secret is gone. Never writes anything to disk, so it can never
    end up committed to Git.
    """

    def __init__(self):
        self._values: dict = {}

    def get(self, key: str) -> Optional[str]:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
# Win32 CREDENTIAL.CredentialBlobSize limit for CRED_PERSIST_LOCAL_MACHINE
# (CRED_MAX_CREDENTIAL_BLOB_SIZE * 5, per the Windows Credential Manager docs).
_MAX_BLOB_BYTES = 512 * 5


if sys.platform == "win32":

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]


class WindowsCredentialSecretStore(SecretStore):
    """Stores secrets in the real Windows Credential Manager (Control
    Panel > Credential Manager > Windows Credentials > Generic
    Credentials), via direct ctypes bindings to advapi32.dll. Each
    secret is one generic credential named f"{credential_prefix}:{key}".

    Windows encrypts the credential blob at rest (DPAPI, bound to the
    logged-in Windows user account) -- this class does no encryption of
    its own and has no plaintext fallback: every failure raises rather
    than silently writing somewhere less secure.

    Not available on non-Windows platforms -- every method raises
    RuntimeError immediately rather than pretending to work.
    """

    def __init__(self, credential_prefix: str = "beanstock/moomoo"):
        self._credential_prefix = credential_prefix

    def _target_name(self, key: str) -> str:
        return f"{self._credential_prefix}:{key}"

    def _advapi32(self):
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsCredentialSecretStore requires Windows (Win32 Credential "
                "Manager via advapi32.dll is not available on this platform)."
            )
        return ctypes.WinDLL("advapi32", use_last_error=True)

    def get(self, key: str) -> Optional[str]:
        advapi32 = self._advapi32()
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
        ]
        cred_ptr = ctypes.POINTER(_CREDENTIAL)()
        ok = advapi32.CredReadW(self._target_name(key), _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
        if not ok:
            err = ctypes.get_last_error()
            if err == _ERROR_NOT_FOUND:
                return None
            raise OSError(f"CredReadW failed (Win32 error {err}) while reading {key!r}.")
        try:
            cred = cred_ptr.contents
            size = cred.CredentialBlobSize
            if size == 0:
                return ""
            raw = bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * size)).contents)
            return raw.decode("utf-8")
        finally:
            advapi32.CredFree.argtypes = [ctypes.c_void_p]
            advapi32.CredFree(cred_ptr)

    def set(self, key: str, value: str) -> None:
        blob = value.encode("utf-8")
        if len(blob) > _MAX_BLOB_BYTES:
            raise ValueError(
                f"Secret for {key!r} is {len(blob)} bytes, exceeding the "
                f"{_MAX_BLOB_BYTES}-byte Windows Credential Manager blob limit."
            )
        advapi32 = self._advapi32()
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]

        blob_buffer = ctypes.create_string_buffer(blob, len(blob)) if blob else ctypes.create_string_buffer(0)
        cred = _CREDENTIAL()
        ctypes.memset(ctypes.byref(cred), 0, ctypes.sizeof(cred))
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = self._target_name(key)
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "beanstock"

        ok = advapi32.CredWriteW(ctypes.byref(cred), 0)
        if not ok:
            raise OSError(f"CredWriteW failed (Win32 error {ctypes.get_last_error()}) while writing {key!r}.")

    def delete(self, key: str) -> None:
        advapi32 = self._advapi32()
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        ok = advapi32.CredDeleteW(self._target_name(key), _CRED_TYPE_GENERIC, 0)
        if not ok:
            err = ctypes.get_last_error()
            if err == _ERROR_NOT_FOUND:
                return  # already absent -- delete is idempotent
            raise OSError(f"CredDeleteW failed (Win32 error {err}) while deleting {key!r}.")
