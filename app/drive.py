"""
Google Drive integration — OAuth + read-only file listing/download.

Requires GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_DRIVE_REDIRECT_URI
in the environment. Tokens are stored in memory per browser session (fine for
local/demo use; use a real store before multi-instance production deploy).
"""

from __future__ import annotations

import io
import os
import secrets
from typing import Any

from fastapi import HTTPException, Request, Response
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SESSION_COOKIE = "pramaan_drive_session"
STATE_COOKIE = "pramaan_drive_oauth_state"

# session_id -> Credentials
_sessions: dict[str, Credentials] = {}
# oauth state -> session_id (short-lived, cleared on callback)
_pending_states: dict[str, str] = {}

MIME_QUERY = (
    "(mimeType='application/pdf' or mimeType contains 'image/') and trashed=false"
)


def is_configured() -> bool:
    return bool(
        os.environ.get("GOOGLE_CLIENT_ID")
        and os.environ.get("GOOGLE_CLIENT_SECRET")
        and os.environ.get("GOOGLE_DRIVE_REDIRECT_URI")
    )


def _redirect_uri() -> str:
    uri = os.environ.get("GOOGLE_DRIVE_REDIRECT_URI", "")
    if not uri:
        raise HTTPException(status_code=503, detail="Google Drive is not configured on this server.")
    return uri


def _client_config() -> dict[str, Any]:
    return {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _create_flow() -> Flow:
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive is not configured on this server.")
    return Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=_redirect_uri())


def _session_id(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def is_connected(request: Request) -> bool:
    sid = _session_id(request)
    return bool(sid and sid in _sessions)


def _credentials(request: Request) -> Credentials:
    sid = _session_id(request)
    if not sid or sid not in _sessions:
        raise HTTPException(status_code=401, detail="Not connected to Google Drive. Please connect first.")
    creds = _sessions[sid]
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        _sessions[sid] = creds
    return creds


def _drive_service(request: Request):
    return build("drive", "v3", credentials=_credentials(request), cache_discovery=False)


def ensure_session_cookie(response: Response) -> str:
    sid = secrets.token_urlsafe(32)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )
    return sid


def start_auth(request: Request, response: Response) -> str:
    """Return the Google OAuth authorization URL."""
    sid = _session_id(request) or ensure_session_cookie(response)
    flow = _create_flow()
    state = secrets.token_urlsafe(32)
    _pending_states[state] = sid
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    response.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600, path="/")
    return auth_url


def finish_auth(request: Request, response: Response, code: str, state: str) -> None:
    """Exchange the OAuth code for credentials and store them for this session."""
    expected_state = request.cookies.get(STATE_COOKIE)
    if not state or state != expected_state or state not in _pending_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state. Please try connecting again.")

    sid = _pending_states.pop(state)
    response.delete_cookie(STATE_COOKIE, path="/")
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )

    flow = _create_flow()
    flow.fetch_token(code=code)
    _sessions[sid] = flow.credentials


def disconnect(request: Request) -> None:
    sid = _session_id(request)
    if sid:
        _sessions.pop(sid, None)


def list_files(request: Request, q: str = "") -> list[dict[str, str]]:
    service = _drive_service(request)
    query = MIME_QUERY
    if q.strip():
        safe = q.replace("'", "\\'")
        query += f" and name contains '{safe}'"

    results: list[dict[str, str]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                pageSize=50,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                orderBy="modifiedTime desc",
                pageToken=page_token,
            )
            .execute()
        )
        for f in resp.get("files", []):
            results.append(
                {
                    "id": f["id"],
                    "name": f.get("name", "Untitled"),
                    "mimeType": f.get("mimeType", ""),
                    "modifiedTime": f.get("modifiedTime", ""),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token or len(results) >= 100:
            break
    return results[:100]


def download_file(request: Request, file_id: str) -> tuple[bytes, str, str]:
    """Return (bytes, mime_type, filename) for a Drive file."""
    service = _drive_service(request)
    meta = service.files().get(fileId=file_id, fields="id, name, mimeType").execute()
    mime = meta.get("mimeType") or "application/octet-stream"
    name = meta.get("name") or file_id

    buf = io.BytesIO()
    request_media = service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request_media)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), mime, name
