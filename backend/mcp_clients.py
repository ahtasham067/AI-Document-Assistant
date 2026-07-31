"""Minimal MCP stdio client helpers for Google Workspace Drive and Gmail tools.

CLI OAuth architecture (Workspace MCP stdio):
  Your script is NOT a web app. When a tool needs Google auth, the MCP child
  process starts a temporary HTTP callback server on localhost:8001. Google
  redirects there after consent; tokens are saved under
  ~/.google_workspace_mcp/credentials/ and reused on later runs.

  Critical: the MCP stdio session must stay open until the browser callback
  finishes — exiting early kills the callback server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

logger = logging.getLogger(__name__)

DEFAULT_DRIVE_MCP_COMMAND = "uvx workspace-mcp --tools drive --tool-tier core"
DEFAULT_EMAIL_MCP_COMMAND = "uvx workspace-mcp --tools gmail --tool-tier core"
# Combined tools so first-time OAuth requests Drive + Gmail scopes together.
DEFAULT_AUTH_MCP_COMMAND = (
    "uvx workspace-mcp --tools drive gmail --tool-tier core"
)

_FILE_ID_RE = re.compile(r"\(ID:\s*([^)]+)\)")
_VIEW_LINK_RE = re.compile(r"View:\s*(\S+)", re.IGNORECASE)
_HTTP_LINK_RE = re.compile(r"https://(?:drive|docs)\.google\.com/\S+")
_AUTH_MARKERS = (
    "ACTION REQUIRED: Google Authentication",
    "Authentication Needed",
    "Authorization URL:",
    "Google Authentication Needed",
)
_MCP_STDERR_LOG = Path.home() / ".google_workspace_mcp" / "logs" / "mcp_stderr.log"


def _is_transient_network_error(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "timed out",
            "timeout",
            "handshake operation timed out",
            "ssl",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "broken pipe",
        )
    )


def _mcp_child_env() -> dict[str, str]:
    """Inherit process env and ensure common uv/uvx install dirs are on PATH."""
    env = dict(os.environ)
    extras = [
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.cargo/bin"),
    ]
    path = env.get("PATH", "")
    for directory in extras:
        if directory and directory not in path.split(os.pathsep):
            path = directory + os.pathsep + path
    env["PATH"] = path
    # Helps stdio OAuth callback / local redirect handling.
    env.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    env.setdefault("MCP_SINGLE_USER_MODE", "1")
    return env


def _server_params_from_command(command_str: str) -> StdioServerParameters:
    parts = shlex.split(command_str)
    if not parts:
        raise ValueError("MCP command is empty")
    return StdioServerParameters(
        command=parts[0],
        args=parts[1:],
        env=_mcp_child_env(),
    )


@contextlib.asynccontextmanager
async def _mcp_session(command_str: str) -> AsyncIterator[ClientSession]:
    """Open an MCP stdio session; MCP server stderr goes to a file (not the terminal)."""
    params = _server_params_from_command(command_str)
    _MCP_STDERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_MCP_STDERR_LOG, "a", encoding="utf-8") as errlog:
        errlog.write(f"\n--- MCP session {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        errlog.flush()
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def _content_text(result: Any) -> str:
    chunks: list[str] = []
    for item in getattr(result, "content", None) or []:
        if isinstance(item, TextContent):
            chunks.append(item.text)
        elif hasattr(item, "text"):
            chunks.append(str(item.text))
        else:
            chunks.append(str(item))
    text = "\n".join(chunks).strip()
    if not text and getattr(result, "structured_content", None):
        text = str(result.structured_content)
    return text


def _is_auth_challenge(text: str, is_error: bool) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _AUTH_MARKERS) or (
        is_error and "Authentication required" in text
    )


def credential_file_for_email(user_google_email: str) -> Path:
    safe = quote(user_google_email, safe="@._-")
    return (
        Path.home()
        / ".google_workspace_mcp"
        / "credentials"
        / f"{safe}.json"
    )


def credentials_ready(user_google_email: str) -> bool:
    path = credential_file_for_email(user_google_email)
    return path.is_file() and path.stat().st_size > 50


async def wait_for_oauth_credentials(
    user_google_email: str,
    *,
    timeout_seconds: int | None = None,
) -> bool:
    """
    Keep the caller (and MCP session) alive until Workspace MCP writes tokens.

    The browser redirects to http://localhost:8001/oauth2callback — that server
    lives inside the MCP child process, so this wait must happen BEFORE the
    stdio session context exits.
    """
    timeout = timeout_seconds or int(os.getenv("GOOGLE_OAUTH_WAIT_SECONDS", "300"))
    path = credential_file_for_email(user_google_email)
    logger.info(
        "Waiting up to %ss for OAuth tokens at %s (complete browser consent; "
        "do not stop this script)",
        timeout,
        path,
    )
    print(
        "\n=== Google OAuth (CLI) ===\n"
        "1. Complete sign-in in the browser (Continue past the unverified-app warning).\n"
        "2. Keep this terminal running — the MCP callback server is on localhost:8001.\n"
        f"3. Waiting for tokens: {path}\n"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if credentials_ready(user_google_email):
            logger.info("OAuth credentials saved for %s", user_google_email)
            print("OAuth complete — credentials saved. Continuing…\n")
            return True
        await asyncio.sleep(2)
    return False


async def _session_call_tool(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    user_google_email: str,
    available: set[str],
    retries: int = 1,
) -> str:
    if tool_name not in available:
        raise RuntimeError(
            f"MCP tool {tool_name!r} is not available. Available: {sorted(available)}"
        )

    last_error: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        logger.info("Calling MCP tool %s (attempt %s/%s)", tool_name, attempt, attempts)
        try:
            result = await session.call_tool(
                tool_name,
                arguments=arguments,
                read_timeout_seconds=180,
            )
            text = _content_text(result)
            is_error = bool(getattr(result, "is_error", False))

            if _is_auth_challenge(text, is_error):
                print(text)
                if not await wait_for_oauth_credentials(user_google_email):
                    raise RuntimeError(
                        "Timed out waiting for Google OAuth. Re-run the script after "
                        "completing browser consent, or increase GOOGLE_OAUTH_WAIT_SECONDS."
                    )
                logger.info("Retrying MCP tool %s after OAuth", tool_name)
                result = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    read_timeout_seconds=180,
                )
                text = _content_text(result)
                is_error = bool(getattr(result, "is_error", False))

            if is_error:
                if _is_transient_network_error(text) and attempt < attempts:
                    logger.info(
                        "%s timed out/network glitch on attempt %s/%s — retrying…",
                        tool_name,
                        attempt,
                        attempts,
                    )
                    print(
                        f"{tool_name} timed out (attempt {attempt}/{attempts}); "
                        "retrying…"
                    )
                    await asyncio.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"MCP tool error: {text or result}")

            if attempt > 1:
                logger.info(
                    "%s succeeded on attempt %s/%s after transient failure",
                    tool_name,
                    attempt,
                    attempts,
                )
                print(f"{tool_name} succeeded on retry (attempt {attempt}/{attempts}).")
            logger.info("MCP tool %s returned %d characters", tool_name, len(text))
            return text
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            if _is_transient_network_error(str(exc)) and attempt < attempts:
                logger.info(
                    "%s exception on attempt %s/%s — retrying… (%s)",
                    tool_name,
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                print(
                    f"{tool_name} timed out (attempt {attempt}/{attempts}); "
                    "retrying…"
                )
                await asyncio.sleep(2 * attempt)
                continue
            raise

    raise RuntimeError(
        f"MCP tool {tool_name} failed after {attempts} attempts: {last_error}"
    )


async def call_mcp_tool(
    command_str: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    user_google_email: str | None = None,
    retries: int = 1,
) -> str:
    """Spawn an MCP server over stdio, call one tool, return text content."""
    email = user_google_email or arguments.get("user_google_email") or ""
    logger.info("Opening MCP session for tool %s", tool_name)
    async with _mcp_session(command_str) as session:
        available = {t.name for t in (await session.list_tools()).tools}
        return await _session_call_tool(
            session,
            tool_name,
            arguments,
            user_google_email=email,
            available=available,
            retries=retries,
        )


def extract_file_id(create_result: str) -> str:
    match = _FILE_ID_RE.search(create_result)
    if match:
        return match.group(1).strip()
    raise ValueError(f"Could not parse Drive file ID from MCP response: {create_result!r}")


def extract_shareable_link(link_result: str) -> str:
    view = _VIEW_LINK_RE.search(link_result)
    if view and view.group(1).startswith("http"):
        return view.group(1).rstrip(".,);")
    http = _HTTP_LINK_RE.search(link_result)
    if http:
        return http.group(0).rstrip(".,);")
    for token in link_result.split():
        if token.startswith("https://drive.google.com") or token.startswith(
            "https://docs.google.com"
        ):
            return token.rstrip(".,);")
    raise ValueError(f"Could not parse shareable link from MCP response: {link_result!r}")


def drive_view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def resolve_drive_link(file_id: str, *texts: str) -> str:
    for text in texts:
        if not text:
            continue
        try:
            return extract_shareable_link(text)
        except ValueError:
            continue
    return drive_view_link(file_id)


async def ensure_google_auth(user_google_email: str) -> None:
    """
    Bootstrap OAuth with Drive+Gmail scopes if tokens are missing.

    Uses a combined MCP server so consent covers upload and email in one go.
    """
    if credentials_ready(user_google_email):
        logger.info("Google credentials already present for %s", user_google_email)
        return

    auth_cmd = os.getenv("AUTH_MCP_COMMAND", DEFAULT_AUTH_MCP_COMMAND)
    logger.info("No stored Google credentials — starting OAuth via MCP (%s)", auth_cmd)
    # Lightweight Drive call triggers auth challenge + callback server.
    await call_mcp_tool(
        auth_cmd,
        "search_drive_files",
        {
            "user_google_email": user_google_email,
            "query": "name contains '___oauth_probe___'",
        },
        user_google_email=user_google_email,
    )


async def upload_summary_to_drive(
    *,
    content: str,
    file_name: str,
    user_google_email: str,
) -> tuple[str, str]:
    """
    Upload text via Drive MCP and return (file_id, shareable_link).

    Prefers the link returned by create_drive_file. get_drive_shareable_link is
    best-effort (retried once); on timeout we fall back to the create link or a
    constructed Drive view URL so a successful upload is never discarded.
    """
    await ensure_google_auth(user_google_email)

    drive_cmd = os.getenv("DRIVE_MCP_COMMAND", DEFAULT_DRIVE_MCP_COMMAND)

    async with _mcp_session(drive_cmd) as session:
        available = {t.name for t in (await session.list_tools()).tools}

        async def _call(
            name: str,
            arguments: dict[str, Any],
            *,
            retries: int = 1,
        ) -> str:
            return await _session_call_tool(
                session,
                name,
                arguments,
                user_google_email=user_google_email,
                available=available,
                retries=retries,
            )

        # create_drive_file can fail on transient SSL handshake timeouts to Google.
        create_text = await _call(
            "create_drive_file",
            {
                "user_google_email": user_google_email,
                "file_name": file_name,
                "content": content,
                "mime_type": "text/plain",
            },
            retries=3,
        )
        file_id = extract_file_id(create_text)
        logger.info("Drive upload created file_id=%s", file_id)

        if "set_drive_file_permissions" in available:
            try:
                await _call(
                    "set_drive_file_permissions",
                    {
                        "user_google_email": user_google_email,
                        "file_id": file_id,
                        "link_sharing": "reader",
                    },
                    retries=2,
                )
            except Exception as exc:
                logger.warning("Could not set link sharing on Drive file: %s", exc)

        link_text = ""
        share_error = None
        try:
            link_text = await _call(
                "get_drive_shareable_link",
                {
                    "user_google_email": user_google_email,
                    "file_id": file_id,
                },
                retries=2,
            )
        except Exception as exc:
            share_error = exc
            logger.warning("get_drive_shareable_link failed: %s", exc)

        shareable_link = resolve_drive_link(file_id, link_text, create_text)
        if share_error:
            logger.warning(
                "Using fallback Drive link after shareable-link failure: %s",
                shareable_link,
            )

    logger.info("Drive shareable link: %s", shareable_link)
    return file_id, shareable_link


async def send_drive_link_email(
    *,
    to: str,
    subject: str,
    body: str,
    user_google_email: str,
) -> str:
    """Send email containing the Drive link via Gmail MCP."""
    email_cmd = os.getenv("EMAIL_MCP_COMMAND", DEFAULT_EMAIL_MCP_COMMAND)
    return await call_mcp_tool(
        email_cmd,
        "send_gmail_message",
        {
            "user_google_email": user_google_email,
            "to": to,
            "subject": subject,
            "body": body,
            "body_format": "plain",
        },
        user_google_email=user_google_email,
    )
