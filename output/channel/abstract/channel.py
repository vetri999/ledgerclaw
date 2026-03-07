"""
Channel Abstract — Base class for all delivery channels.

Structure:
  1. Setup    — first-time pairing, config persistence, setup-complete check
  2. Connect  — connect, disconnect, connection lifecycle
  3. Send     — format, split, deliver messages
  4. Receive  — listen for incoming messages

Scaling: To add a new channel (Slack, Telegram, etc.):
  1. Subclass this
  2. Implement the 5 _platform methods (marked with "IMPLEMENT THIS")
  3. Everything else (formatting, splitting, config, subprocess) is inherited

Bridge utilities (for non-Python channels like Node.js):
  call_subprocess, stream_subprocess
  — available to any subclass that needs them
"""

import json
import os
import re
import subprocess
import time
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional


class Channel(ABC):

    def __init__(self, spec: dict, user_dir: str):
        """
        spec     — parsed YAML config (group name, format rules, paths, etc.)
        user_dir — absolute path to user/ folder for this channel (auth, config.json)
        """
        self.spec = spec
        self.user_dir = os.path.abspath(user_dir)
        self.config_path = os.path.join(self.user_dir, "config.json")

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def load_config(self) -> Optional[dict]:
        """Read channel's saved state (group JID, etc.)"""
        if not os.path.exists(self.config_path):
            return None
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def save_config(self, data: dict) -> None:
        """Write channel state to config.json"""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def is_setup_complete(self) -> bool:
        """Check if config exists with all required fields"""
        config = self.load_config()
        if not config:
            return False
        required = self.spec.get("setup", {}).get("required_config_fields", [])
        return all(field in config for field in required)

    def setup(self) -> bool:
        """Full setup flow. Skips if already done. Returns True on success."""
        if self.is_setup_complete():
            config = self.load_config()
            print(f"Channel already set up: {config.get('group_name', 'unknown')}")
            return True

        print(f"Setting up {self.spec['name']} channel...")
        success = self._platform_setup()

        if success and self.is_setup_complete():
            print("Channel setup complete.")
            return True

        print("Setup did not complete. Check messages above.")
        return False

    @abstractmethod
    def _platform_setup(self) -> bool:
        """IMPLEMENT THIS — Platform-specific setup (QR scan, OAuth, etc.)"""
        pass

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Validate setup, then delegate to platform."""
        if not self.is_setup_complete():
            print("Channel not set up. Run setup() first.")
            return False

        timeout_ms = self.spec.get("connect", {}).get("timeout_ms", 30000)
        try:
            return self._platform_connect(timeout_ms)
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    def disconnect(self) -> None:
        """Graceful disconnect."""
        try:
            self._platform_disconnect()
        except Exception as e:
            print(f"Disconnect error (non-fatal): {e}")

    @abstractmethod
    def _platform_connect(self, timeout_ms: int) -> bool:
        """IMPLEMENT THIS"""
        pass

    @abstractmethod
    def _platform_disconnect(self) -> None:
        """IMPLEMENT THIS"""
        pass

    # ─────────────────────────────────────────────────────────────
    # 3. SEND
    # ─────────────────────────────────────────────────────────────

    def send(self, message: str) -> bool:
        """Full send flow: format → split → deliver each part."""
        if not message.strip():
            print("Nothing to send (empty message).")
            return False

        if not self.is_setup_complete():
            print("Channel not set up. Run setup() first.")
            return False

        # Format markdown → platform text
        formatted = self.format(message)

        # Split if too long
        max_len = self.spec.get("formatting", {}).get("max_message_length", 2000)
        parts = self.split_message(formatted, max_len)

        # Send each part
        for i, part in enumerate(parts):
            if len(parts) > 1:
                part = f"_({i + 1}/{len(parts)})_\n\n{part}"

            success = self._platform_send(part)
            if not success:
                print(f"Failed to send part {i + 1}/{len(parts)}.")
                return False

            if i < len(parts) - 1:
                time.sleep(1)

        return True

    def format(self, markdown: str) -> str:
        """
        Convert markdown to platform format using rules from YAML.
        Universal engine — platforms only supply the rules dict in their YAML.
        """
        text = markdown
        rules = self.spec.get("formatting", {}).get("rules", {})

        if "heading" in rules:
            h_fmt = rules["heading"]
            text = re.sub(
                r"^##\s+(.+)$",
                lambda m: h_fmt.replace("{text}", m.group(1)),
                text, flags=re.MULTILINE,
            )

        if "sub_heading" in rules:
            sh_fmt = rules["sub_heading"]
            text = re.sub(
                r"^###\s+(.+)$",
                lambda m: sh_fmt.replace("{text}", m.group(1)),
                text, flags=re.MULTILINE,
            )

        if "bold" in rules:
            b_fmt = rules["bold"]
            text = re.sub(
                r"\*\*(.+?)\*\*",
                lambda m: b_fmt.replace("{text}", m.group(1)),
                text,
            )

        if "bullet" in rules:
            text = re.sub(r"^- ", rules["bullet"], text, flags=re.MULTILINE)

        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def split_message(self, text: str, max_length: int) -> list[str]:
        """Split long message at section boundaries (bold headings)."""
        if len(text) <= max_length:
            return [text]

        sections = re.split(r"(?=^\*.+\*$)", text, flags=re.MULTILINE)
        messages = []
        current = ""

        for section in sections:
            if current and len(current) + len(section) > max_length:
                messages.append(current.strip())
                current = section
            else:
                current += section

        if current.strip():
            messages.append(current.strip())

        return messages

    @abstractmethod
    def _platform_send(self, text: str) -> bool:
        """IMPLEMENT THIS — Send one raw text message."""
        pass

    # ─────────────────────────────────────────────────────────────
    # 4. RECEIVE
    # ─────────────────────────────────────────────────────────────

    def receive(self, callback: Callable[[dict], None]) -> None:
        """
        Listen for incoming messages. Blocking call.
        Calls callback({"from": "...", "text": "...", "timestamp": "..."}) for each.
        """
        if not self.is_setup_complete():
            print("Channel not set up. Run setup() first.")
            return

        print(f"Listening on {self.spec['name']}...")
        try:
            self._platform_listen(callback)
        except KeyboardInterrupt:
            print("Stopped listening.")
        except Exception as e:
            print(f"Listen error: {e}")

    @abstractmethod
    def _platform_listen(self, callback: Callable[[dict], None]) -> None:
        """IMPLEMENT THIS — Listen for messages, call callback for each one."""
        pass

    # ─────────────────────────────────────────────────────────────
    # BRIDGE UTILITIES (for non-Python channels)
    # ─────────────────────────────────────────────────────────────

    def call_subprocess(
        self, command: str, cwd: str, timeout: int = 300,
        pass_through: bool = False, input_text: str = None,
    ) -> tuple[int, str, str]:
        """
        Run a subprocess command, wait for exit.
        pass_through=True → print stdout/stderr live (for QR codes, etc.)
        input_text → pipe text to stdin
        Returns (return_code, stdout, stderr).
        """
        if pass_through:
            proc = subprocess.Popen(command, shell=True, cwd=cwd)
            proc.wait(timeout=timeout)
            return (proc.returncode, "", "")

        proc = subprocess.run(
            command, shell=True, cwd=cwd,
            input=input_text,
            capture_output=True, text=True, timeout=timeout,
        )
        return (proc.returncode, proc.stdout, proc.stderr)

    def stream_subprocess(
        self, command: str, cwd: str,
        line_callback: Callable[[str], None],
    ) -> subprocess.Popen:
        """
        Start a long-running subprocess.
        Calls line_callback(line) for each stdout line.
        Returns Popen object (caller can .terminate() it).
        """
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def _read_lines():
            for line in proc.stdout:
                stripped = line.strip()
                if stripped:
                    line_callback(stripped)

        thread = threading.Thread(target=_read_lines, daemon=True)
        thread.start()
        return proc
