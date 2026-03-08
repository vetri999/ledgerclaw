"""
Source Abstract — Base class for all data sources.

Structure:
  1. Setup     — first-time auth (OAuth, API key, etc.), credential persistence
  2. Connect   — establish connection, refresh tokens if needed
  3. Fetch     — retrieve records, normalize to StandardRecord format

Scaling: To add a new source (bank API, SMS, CSV, etc.):
  1. Subclass this
  2. Implement the 4 _platform methods (marked with "IMPLEMENT THIS")
  3. Everything else (config, sync state, StandardRecord) is inherited

StandardRecord — the universal format all sources output.
  Every source converts its raw data into this shape.
  Downstream (store, agents, tools) only sees StandardRecords.
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class StandardRecord:
    """Universal record format. Every source outputs this shape."""
    id: str                    # unique ID from source (e.g. Gmail message ID)
    source: str                # "gmail", "bank_api", "sms", etc.
    sender: str                # who sent it
    subject: str               # title / summary
    body: str                  # full text content
    timestamp: datetime        # when the record was created at source
    metadata: dict             # source-specific extras (labels, attachments, etc.)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


class Source(ABC):

    def __init__(self, spec: dict, user_dir: str):
        """
        spec     — parsed YAML config (scopes, batch size, retry config, etc.)
        user_dir — absolute path to user/ folder for this source (credentials, tokens)
        """
        self.spec = spec
        self.user_dir = os.path.abspath(user_dir)

        # Sync state file — tracks where we left off (e.g. last historyId)
        self.sync_state_path = os.path.join(self.user_dir, "sync_state.json")

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def load_sync_state(self) -> Optional[dict]:
        """Read sync state (last fetch position, etc.)"""
        if not os.path.exists(self.sync_state_path):
            return None
        try:
            with open(self.sync_state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def save_sync_state(self, state: dict) -> None:
        """Write sync state"""
        os.makedirs(os.path.dirname(self.sync_state_path), exist_ok=True)
        with open(self.sync_state_path, "w") as f:
            json.dump(state, f, indent=2)

    def is_setup_complete(self) -> bool:
        """Check if source has been authenticated (credentials exist)"""
        required = self.spec.get("setup", {}).get("required_files", [])
        return all(
            os.path.exists(os.path.join(self.user_dir, f))
            for f in required
        )

    def setup(self) -> bool:
        """
        Full setup flow. Skips if already done.
        Returns True if setup is complete (new or existing).
        """
        if self.is_setup_complete():
            print(f"Source already set up: {self.spec['name']}")
            return True

        print(f"Setting up {self.spec['name']} source...")
        success = self._platform_setup()

        if success and self.is_setup_complete():
            print("Source setup complete.")
            return True

        print("Setup did not complete. Check messages above.")
        return False

    @abstractmethod
    def _platform_setup(self) -> bool:
        """IMPLEMENT THIS — Platform-specific auth flow (OAuth, API key, etc.)"""
        pass

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Validate setup, then establish connection."""
        if not self.is_setup_complete():
            print("Source not set up. Run setup() first.")
            return False

        try:
            return self._platform_connect()
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    @abstractmethod
    def _platform_connect(self) -> bool:
        """IMPLEMENT THIS — Establish connection, refresh tokens if needed."""
        pass

    # ─────────────────────────────────────────────────────────────
    # 3. FETCH
    # ─────────────────────────────────────────────────────────────

    def fetch(self, since: datetime = None, until: datetime = None) -> list[StandardRecord]:
        """
        Fetch records from source.
        - If since/until provided, fetch that range
        - If not, uses sync state to fetch incrementally (since last fetch)
        - If no sync state, fetches last N days (from spec: initial_fetch_days)

        Returns list of StandardRecords. Updates sync state after fetch.
        """
        if not self.connect():
            return []

        # Determine time range
        if since is None:
            sync = self.load_sync_state()
            if sync and "last_fetch_timestamp" in sync:
                since = datetime.fromisoformat(sync["last_fetch_timestamp"])
            else:
                # First run — fetch initial_fetch_days
                days = self.spec.get("fetch", {}).get("initial_fetch_days", 90)
                from datetime import timedelta
                since = datetime.now() - timedelta(days=days)

        if until is None:
            until = datetime.now()

        print(f"Fetching from {self.spec['name']}: {since.date()} → {until.date()}")

        try:
            records = self._platform_fetch(since, until)
            print(f"Fetched {len(records)} records.")

            # Update sync state
            self.save_sync_state({
                "last_fetch_timestamp": until.isoformat(),
                "last_fetch_count": len(records),
            })

            return records

        except Exception as e:
            print(f"Fetch failed: {e}")
            return []

    @abstractmethod
    def _platform_fetch(self, since: datetime, until: datetime) -> list[StandardRecord]:
        """IMPLEMENT THIS — Fetch and normalize records from the platform."""
        pass
