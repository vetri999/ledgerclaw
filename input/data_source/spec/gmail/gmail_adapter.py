"""
Gmail Adapter — Implements Source abstract for Gmail.

Structure mirrors source.py:
  1. Setup    — OAuth2 flow (open browser, user grants permission, save token)
  2. Connect  — load token, refresh if expired, build Gmail API service
  3. Fetch    — incremental via historyId, batch processing, MIME → StandardRecord

Credentials flow:
  - User places credentials.json in user/gmail/ (downloaded from Google Cloud Console)
  - First run: browser opens, user grants permission, token.json is saved
  - Subsequent runs: token.json is loaded and refreshed silently
  - Token refresh is automatic — user is never asked again unless they revoke access
"""

import base64
import email
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# Add project root to path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _project_root)

from input.data_source.abstract.source import Source, StandardRecord

# Google libraries
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


class GmailSource(Source):

    def __init__(self, spec: dict, user_dir: str):
        super().__init__(spec, user_dir)

        self.credentials_path = os.path.join(self.user_dir, "credentials.json")
        self.token_path = os.path.join(self.user_dir, "token.json")
        self.scopes = spec.get("setup", {}).get("scopes", [])

        # Gmail API service — set during connect()
        self.service = None

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP — OAuth2 flow
    # ─────────────────────────────────────────────────────────────

    def _platform_setup(self) -> bool:
        """
        Run OAuth2 flow:
          1. Read credentials.json (user must place it in user/gmail/)
          2. Open browser → user grants permission
          3. Save token.json for future use
        """
        if not os.path.exists(self.credentials_path):
            print(f"Missing: {self.credentials_path}")
            print("Download OAuth credentials from Google Cloud Console")
            print("and place the file as 'credentials.json' in:")
            print(f"  {self.user_dir}/")
            return False

        # Check if token already exists and is valid
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)
            if creds and creds.valid:
                print("Token already valid.")
                return True
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save_token(creds)
                    print("Token refreshed.")
                    return True
                except Exception:
                    print("Token refresh failed. Re-authenticating...")

        # Run OAuth flow — opens browser
        print("Opening browser for Google authentication...")
        print("Grant LedgerClaw permission to access your Gmail.")
        print("")

        flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_path,
            scopes=self.scopes,
            redirect_uri="http://localhost:3377/oauth2callback",
        )

        creds = flow.run_local_server(
            port=3377,
            prompt="consent",            # always show consent screen
            access_type="offline",        # get refresh token
        )

        self._save_token(creds)
        print("Gmail authentication successful.")
        return True

    def _save_token(self, creds: Credentials) -> None:
        """Save OAuth token to user dir."""
        os.makedirs(self.user_dir, exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write(creds.to_json())

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT — load token, build API service
    # ─────────────────────────────────────────────────────────────

    def _platform_connect(self) -> bool:
        """Load token, refresh if expired, build Gmail API service."""
        if not os.path.exists(self.token_path):
            print("No token found. Run setup first.")
            return False

        creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)

        # Refresh if expired
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
            except Exception as e:
                print(f"Token refresh failed: {e}")
                print("Run setup again to re-authenticate.")
                return False

        if not creds.valid:
            print("Token is invalid. Run setup again.")
            return False

        # Build Gmail API service
        self.service = build("gmail", "v1", credentials=creds)
        return True

    # ─────────────────────────────────────────────────────────────
    # 3. FETCH — retrieve emails, normalize to StandardRecord
    # ─────────────────────────────────────────────────────────────

    def _platform_fetch(self, since: datetime, until: datetime) -> list[StandardRecord]:
        """
        Fetch emails from Gmail in the given time range.
        Uses batching (batch_size from spec) with delay between batches.
        Returns list of StandardRecords.
        """
        if not self.service:
            print("Not connected. Call connect() first.")
            return []

        batch_size = self.spec.get("fetch", {}).get("batch_size", 25)
        batch_delay = self.spec.get("fetch", {}).get("batch_delay_seconds", 1)
        max_retries = self.spec.get("fetch", {}).get("max_retries", 3)

        # — Step 1: Get message IDs in time range —

        # Gmail uses epoch seconds for after/before queries
        after_epoch = int(since.timestamp())
        before_epoch = int(until.timestamp())
        query = f"after:{after_epoch} before:{before_epoch}"

        message_ids = []
        page_token = None

        while True:
            result = self._api_call_with_retry(
                lambda pt=page_token: self.service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=batch_size,
                    pageToken=pt,
                ).execute(),
                max_retries=max_retries,
            )

            if not result:
                break

            messages = result.get("messages", [])
            message_ids.extend([m["id"] for m in messages])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

            time.sleep(batch_delay)

        if not message_ids:
            return []

        print(f"Found {len(message_ids)} emails. Fetching details...")

        # — Step 2: Fetch full message details in batches —

        records = []
        for i in range(0, len(message_ids), batch_size):
            batch = message_ids[i : i + batch_size]

            for msg_id in batch:
                record = self._fetch_one_message(msg_id, max_retries)
                if record:
                    records.append(record)

            # Delay between batches
            if i + batch_size < len(message_ids):
                time.sleep(batch_delay)

        return records

    def _fetch_one_message(self, msg_id: str, max_retries: int) -> Optional[StandardRecord]:
        """Fetch one email by ID, parse MIME, return StandardRecord."""
        result = self._api_call_with_retry(
            lambda: self.service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full",
            ).execute(),
            max_retries=max_retries,
        )

        if not result:
            return None

        try:
            # Extract headers
            headers = {h["name"].lower(): h["value"] for h in result.get("payload", {}).get("headers", [])}
            sender = headers.get("from", "unknown")
            subject = headers.get("subject", "(no subject)")
            date_str = headers.get("date", "")

            # Parse timestamp
            timestamp = self._parse_date(date_str, result.get("internalDate"))

            # Extract body text
            body = self._extract_body(result.get("payload", {}))

            # Extract labels and other metadata
            labels = result.get("labelIds", [])

            return StandardRecord(
                id=msg_id,
                source="gmail",
                sender=self._extract_email_address(sender),
                subject=subject,
                body=body,
                timestamp=timestamp,
                metadata={
                    "sender_full": sender,
                    "labels": labels,
                    "snippet": result.get("snippet", ""),
                    "thread_id": result.get("threadId", ""),
                },
            )

        except Exception as e:
            print(f"Failed to parse message {msg_id}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # HELPERS — parsing, retry, etc.
    # ─────────────────────────────────────────────────────────────

    def _extract_body(self, payload: dict) -> str:
        """
        Walk MIME parts, extract plain text body.
        Falls back to HTML (stripped of tags) if no plain text.
        """
        # Direct body (simple messages)
        if payload.get("body", {}).get("data"):
            mime_type = payload.get("mimeType", "")
            data = payload["body"]["data"]
            text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if "html" in mime_type:
                return self._strip_html(text)
            return text

        # Multipart — walk parts recursively
        parts = payload.get("parts", [])
        plain_text = ""
        html_text = ""

        for part in parts:
            mime_type = part.get("mimeType", "")

            if mime_type == "text/plain" and part.get("body", {}).get("data"):
                data = part["body"]["data"]
                plain_text += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

            elif mime_type == "text/html" and part.get("body", {}).get("data"):
                data = part["body"]["data"]
                html_text += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

            elif part.get("parts"):
                # Nested multipart — recurse
                nested = self._extract_body(part)
                if nested:
                    plain_text += nested

        if plain_text:
            return plain_text.strip()
        if html_text:
            return self._strip_html(html_text).strip()

        return ""

    def _strip_html(self, html: str) -> str:
        """Simple HTML tag stripper. Good enough for email bodies."""
        import re
        # Remove style and script blocks
        text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Replace br and p with newlines
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", "", text)
        # Decode common HTML entities
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&quot;", '"').replace("&#39;", "'")
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()

    def _extract_email_address(self, sender: str) -> str:
        """Extract clean email from 'Name <email@domain.com>' format."""
        import re
        match = re.search(r"<(.+?)>", sender)
        if match:
            return match.group(1).lower()
        return sender.strip().lower()

    def _parse_date(self, date_str: str, internal_date_ms: str = None) -> datetime:
        """Parse email date header. Falls back to Gmail's internalDate."""
        if internal_date_ms:
            try:
                return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(date_str)
            except Exception:
                pass

        return datetime.now(tz=timezone.utc)

    def _api_call_with_retry(self, call, max_retries: int = 3):
        """
        Execute a Gmail API call with retry on rate limits and server errors.
        Exponential backoff: 2s, 4s, 8s.
        """
        for attempt in range(max_retries):
            try:
                return call()
            except Exception as e:
                error_str = str(e)
                # Retry on rate limits (429) and server errors (5xx)
                if "429" in error_str or "500" in error_str or "503" in error_str:
                    wait = 2 ** (attempt + 1)
                    print(f"API error (attempt {attempt + 1}/{max_retries}), retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                # Non-retryable error
                raise
        print(f"API call failed after {max_retries} retries.")
        return None
