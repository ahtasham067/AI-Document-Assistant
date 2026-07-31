import argparse
import asyncio
import logging
import os
import sys
import time

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
_PROJECT_VENV_PYTHON = os.path.join(_PROJECT_ROOT, ".venv", "bin", "python")

if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


# Prefer this project's .venv when required deps are missing (wrong/foreign venv or system Python).
def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


if not _mcp_available():
    # Compare venv paths without realpath — both venvs symlink to the same base
    # interpreter (e.g. /usr/bin/python3.12), so realpath would falsely match.
    using_project_venv = os.path.realpath(sys.prefix) == os.path.realpath(
        os.path.join(_PROJECT_ROOT, ".venv")
    )
    if os.path.isfile(_PROJECT_VENV_PYTHON) and not using_project_venv:
        os.execv(_PROJECT_VENV_PYTHON, [_PROJECT_VENV_PYTHON, *sys.argv])
    raise SystemExit(
        "Missing dependency 'mcp'. Install project deps with:\n"
        f"  {_PROJECT_VENV_PYTHON} -m pip install -r requirements.txt\n"
        f"Then run: {_PROJECT_VENV_PYTHON} backend/search_and_summarize.py ..."
    )

import ollama
from dotenv import load_dotenv

from mcp_clients import send_drive_link_email, upload_summary_to_drive

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def search_topic(topic):
    # Simple placeholder - in real project, use requests + a search API (like Serper, Tavily, etc.)
    return f"Sample search results about {topic}: AI agents are systems that can plan and act autonomously..."


def save_to_file(content, filename):
    with open(filename, "w") as f:
        f.write(content)
    return f"Saved to {filename}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Search a topic, summarize with Ollama, save locally, "
        "upload to Google Drive via MCP, and email the shareable link.",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Topic to search and summarize",
    )
    return parser.parse_args(argv)


def resolve_topic(args) -> str:
    if args.topic:
        return args.topic.strip()
    try:
        topic = input("Enter topic to search and summarize: ").strip()
    except EOFError:
        topic = ""
    if not topic:
        raise SystemExit("A topic is required.")
    return topic


def _oauth_config_status() -> dict:
    """Return presence flags for Workspace MCP OAuth client config (never secrets)."""
    client_id = bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip())
    client_secret = bool(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip())
    secrets_path = (
        os.getenv("GOOGLE_CLIENT_SECRET_PATH")
        or os.getenv("GOOGLE_CLIENT_SECRETS")
        or ""
    ).strip()
    secrets_file_ok = bool(secrets_path) and os.path.isfile(secrets_path)
    return {
        "has_client_id": client_id,
        "has_client_secret": client_secret,
        "has_secrets_path_env": bool(secrets_path),
        "secrets_file_exists": secrets_file_ok,
        "configured": (client_id and client_secret) or secrets_file_ok,
    }


def _oauth_setup_message() -> str:
    return (
        "Google OAuth client credentials are missing for Workspace MCP.\n"
        "Add to .env (from Google Cloud Console → APIs & Services → Credentials → "
        "OAuth 2.0 Client ID, type Desktop or Web):\n"
        "  GOOGLE_OAUTH_CLIENT_ID=...\n"
        "  GOOGLE_OAUTH_CLIENT_SECRET=...\n"
        "Enable Google Drive API and Gmail API for that project.\n"
        "Authorized redirect URI: http://localhost:8001/oauth2callback\n"
        "If browser shows 403 access_denied / 'verification process': add "
        "GOOGLE_USER_EMAIL as a Test user on the OAuth consent screen "
        "(Audience → Test users), then retry.\n"
        "See: https://workspacemcp.com/docs and .env.example"
    )


def _oauth_tester_message() -> str:
    return (
        "Google blocked sign-in (403 access_denied): the OAuth app is in Testing mode.\n"
        "Fix in Google Cloud Console for this project:\n"
        "  1. APIs & Services → OAuth consent screen\n"
        "  2. Under Audience / Test users, add: your GOOGLE_USER_EMAIL\n"
        "  3. Save, wait ~1 minute, run the script again and approve in the browser\n"
        "You do not need Google verification for personal/testing use."
    )


def _exception_root_text(exc: BaseException) -> str:
    """Flatten ExceptionGroup / nested causes into a readable string."""
    parts: list[str] = [str(exc)]
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            parts.append(_exception_root_text(nested))
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        parts.append(_exception_root_text(cause))
    return "\n".join(p for p in parts if p)


def _gmail_api_disabled_message() -> str:
    return (
        "Gmail API is not enabled for your Google Cloud project.\n"
        "Enable it here (same project as your OAuth client):\n"
        "  https://console.developers.google.com/apis/api/gmail.googleapis.com/overview\n"
        "Also ensure Drive API is enabled. Wait 1–2 minutes, then re-run the script.\n"
        "Drive upload already worked; only the email step needs Gmail API."
    )


def _display_email_status(raw_email: str | None) -> str:
    if not raw_email:
        return "(not available)"
    if raw_email == "sent":
        return "Sent"
    if raw_email.startswith("failed:") or raw_email.startswith("skipped:"):
        return raw_email
    # MCP success payloads vary; treat non-failure text as Sent for the API.
    return "Sent"


def _build_result(
    *,
    topic: str,
    summary: str | None,
    publish: dict | None,
    processing_time_seconds: float,
    error: str | None = None,
) -> dict:
    publish = publish or {}
    drive_link = publish.get("drive_link")
    raw_email = publish.get("email_status")

    if drive_link:
        upload_status = "Success"
    elif isinstance(raw_email, str) and raw_email.startswith("skipped: Drive upload failed"):
        upload_status = "Failed"
    elif isinstance(raw_email, str) and raw_email.startswith("skipped:"):
        upload_status = "Skipped"
    elif error and not summary:
        upload_status = "Not started"
    else:
        upload_status = "Failed"

    email_failed_or_skipped = isinstance(raw_email, str) and (
        raw_email.startswith("failed:") or raw_email.startswith("skipped:")
    )
    success = bool(drive_link) and not email_failed_or_skipped and error is None

    if error is None and email_failed_or_skipped and isinstance(raw_email, str):
        error = raw_email

    return {
        "success": success,
        "topic": topic,
        "summary": summary,
        "google_drive_link": drive_link,
        "upload_status": upload_status,
        "email_status": _display_email_status(raw_email),
        "processing_time_seconds": round(processing_time_seconds, 2),
        "error": error,
    }


async def publish_and_notify(summary: str, topic: str, filename: str) -> dict:
    """Upload via Drive MCP and notify via Email MCP. Returns status dict."""
    google_user = os.getenv("GOOGLE_USER_EMAIL", "").strip()
    notify_email = os.getenv("NOTIFY_EMAIL", "").strip()
    oauth = _oauth_config_status()

    result = {
        "drive_file_id": None,
        "drive_link": None,
        "email_status": None,
    }

    if not google_user:
        result["email_status"] = "skipped: GOOGLE_USER_EMAIL is not set"
        logger.error(
            "GOOGLE_USER_EMAIL is required for Drive/Email MCP. "
            "Set it in .env (see .env.example). Complete Workspace MCP OAuth first."
        )
        return result

    if not oauth["configured"]:
        msg = _oauth_setup_message()
        logger.error(msg)
        result["email_status"] = "skipped: Google OAuth client credentials not configured"
        print(msg)
        return result

    try:
        logger.info("Uploading %s to Google Drive via MCP", filename)
        file_id, link = await upload_summary_to_drive(
            content=summary,
            file_name=os.path.basename(filename),
            user_google_email=google_user,
        )
        result["drive_file_id"] = file_id
        result["drive_link"] = link
        logger.info("Drive upload succeeded: %s", link)
    except Exception as exc:
        logger.exception("Drive MCP upload failed: %s", exc)
        root = _exception_root_text(exc)
        if "access_denied" in root.lower() or "verification process" in root.lower():
            print(_oauth_tester_message())
        elif "OAuth" in root or "client credentials" in root:
            print(_oauth_setup_message())
        elif any(
            m in root.lower()
            for m in ("timed out", "handshake", "ssl", "connection reset")
        ):
            print(
                "Google Drive API network/SSL timeout after retries.\n"
                "Check internet connectivity and try again in a minute.\n"
                "If this keeps happening, try another network or disable VPN temporarily."
            )
        result["email_status"] = f"skipped: Drive upload failed ({root[:400]})"
        return result

    if not notify_email:
        result["email_status"] = "skipped: NOTIFY_EMAIL is not set"
        logger.error("NOTIFY_EMAIL is required to send the notification email.")
        return result

    subject = f"Summary ready: {topic}"
    body = (
        f"Your document summary for \"{topic}\" is ready.\n\n"
        f"Google Drive link:\n{link}\n"
    )
    try:
        logger.info("Sending notification email to %s via Email MCP", notify_email)
        email_result = await send_drive_link_email(
            to=notify_email,
            subject=subject,
            body=body,
            user_google_email=google_user,
        )
        result["email_status"] = email_result or "sent"
        logger.info("Email MCP send succeeded")
    except Exception as exc:
        root = _exception_root_text(exc)
        gmail_api_off = (
            "gmail.googleapis.com" in root.lower()
            or "accessnotconfigured" in root.lower()
            or "gmail api has not been used" in root.lower()
        )
        logger.exception("Email MCP send failed: %s", root)
        if gmail_api_off:
            print(_gmail_api_disabled_message())
            result["email_status"] = (
                "failed: Gmail API not enabled in Google Cloud project "
                "(enable gmail.googleapis.com, then retry)"
            )
        else:
            result["email_status"] = f"failed: {root[:500]}"

    return result


async def run_workflow(topic: str) -> dict:
    """Run search → summarize → save → Drive upload → email. Returns API-shaped dict."""
    topic = (topic or "").strip()
    if not topic:
        return _build_result(
            topic="",
            summary=None,
            publish=None,
            processing_time_seconds=0.0,
            error="A topic is required.",
        )

    started = time.perf_counter()
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    filename = os.path.join(_PROJECT_ROOT, "summary.txt")

    try:
        logger.info("Searching topic: %s", topic)
        search_results = search_topic(topic)

        logger.info("Summarizing with Ollama model %s", model)
        summary_response = ollama.chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": f"Summarize this in 2 sentences:\n\n{search_results}",
                }
            ],
        )
        summary = summary_response["message"]["content"]

        save_result = save_to_file(summary, filename)
        logger.info(save_result)

        publish = await publish_and_notify(summary, topic, filename)
        elapsed = time.perf_counter() - started
        return _build_result(
            topic=topic,
            summary=summary,
            publish=publish,
            processing_time_seconds=elapsed,
        )
    except Exception as exc:
        logger.exception("Workflow failed: %s", exc)
        elapsed = time.perf_counter() - started
        return _build_result(
            topic=topic,
            summary=None,
            publish=None,
            processing_time_seconds=elapsed,
            error=_exception_root_text(exc)[:800],
        )


def main(argv=None):
    args = parse_args(argv)
    topic = resolve_topic(args)
    result = asyncio.run(run_workflow(topic))

    if result.get("summary"):
        print("Summary:", result["summary"])
        print(f"Saved to {os.path.join(_PROJECT_ROOT, 'summary.txt')}")

    drive_link = result.get("google_drive_link") or "(not available)"
    email_status = result.get("email_status") or "(not available)"
    print("Drive link:", drive_link)
    print("Email status:", email_status)
    if result.get("error"):
        print("Error:", result["error"])

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
