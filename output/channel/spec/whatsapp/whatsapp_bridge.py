"""
WhatsApp Bridge — Implements Channel abstract via Node.js subprocess.

Structure mirrors channel.py:
  1. Setup    — calls whatsapp.ts "setup" (QR scan, group find)
  2. Connect  — no-op (whatsapp.ts connects per-command)
  3. Send     — calls whatsapp.ts "send" with text via stdin
  4. Receive  — streams whatsapp.ts "listen", parses JSON lines

This file is thin glue. All heavy lifting is in:
  - channel.py (formatting, splitting, config, subprocess utils)
  - whatsapp.ts (raw Baileys operations)

KEY: All paths passed to whatsapp.ts are ABSOLUTE.
     whatsapp.ts runs in a different cwd, so relative paths break.
"""

import json
import os
import sys
from typing import Callable

# Add project root to path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _project_root)

from output.channel.abstract.channel import Channel


class WhatsAppChannel(Channel):

    def __init__(self, spec: dict, user_dir: str):
        super().__init__(spec, user_dir)

        # Resolve Node.js project dir (where package.json lives)
        # This is the same folder as this bridge file
        self.node_dir = os.path.abspath(os.path.dirname(__file__))

        # Auth dir inside user folder (absolute)
        self.auth_dir = os.path.join(self.user_dir, "auth")

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def _platform_setup(self) -> bool:
        """Call whatsapp.ts setup — QR scan, find group, save config."""
        os.makedirs(self.auth_dir, exist_ok=True)

        group_name = self.spec["setup"]["group_name"]
        qr_timeout = self.spec["setup"]["qr_timeout_ms"]

        # All paths are absolute — whatsapp.ts runs in node_dir
        cmd = (
            f'npx tsx whatsapp.ts setup'
            f' --auth-dir "{self.auth_dir}"'
            f' --config-path "{self.config_path}"'
            f' --group-name "{group_name}"'
            f' --qr-timeout {qr_timeout}'
        )

        # pass_through=True so user sees QR code in terminal
        returncode, _, _ = self.call_subprocess(
            cmd, cwd=self.node_dir, timeout=300, pass_through=True
        )

        return returncode == 0

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def _platform_connect(self, timeout_ms: int) -> bool:
        """No-op — whatsapp.ts connects fresh per command."""
        return True

    def _platform_disconnect(self) -> None:
        """No-op — whatsapp.ts disconnects after each command."""
        pass

    # ─────────────────────────────────────────────────────────────
    # 3. SEND
    # ─────────────────────────────────────────────────────────────

    def _platform_send(self, text: str) -> bool:
        """Call whatsapp.ts send — pipe message via stdin."""
        config = self.load_config()
        if not config:
            print("No config found. Run setup first.")
            return False

        group_jid = config["group_jid"]
        timeout = self.spec["connect"]["timeout_ms"]

        cmd = (
            f'npx tsx whatsapp.ts send'
            f' --auth-dir "{self.auth_dir}"'
            f' --group-jid "{group_jid}"'
            f' --timeout {timeout}'
        )

        returncode, stdout, stderr = self.call_subprocess(
            cmd, cwd=self.node_dir, timeout=60, input_text=text
        )

        if returncode != 0:
            print(f"Send failed: {stderr.strip()}")
            return False

        return True

    # ─────────────────────────────────────────────────────────────
    # 4. RECEIVE
    # ─────────────────────────────────────────────────────────────

    def _platform_listen(self, callback: Callable[[dict], None]) -> None:
        """Stream whatsapp.ts listen — parse JSON lines from stdout."""
        config = self.load_config()
        if not config:
            print("No config found. Run setup first.")
            return

        group_jid = config["group_jid"]

        cmd = (
            f'npx tsx whatsapp.ts listen'
            f' --auth-dir "{self.auth_dir}"'
            f' --group-jid "{group_jid}"'
        )

        def on_line(line: str):
            try:
                message = json.loads(line)
                callback(message)
            except json.JSONDecodeError:
                pass  # Not JSON — log output from whatsapp.ts, ignore

        proc = self.stream_subprocess(cmd, cwd=self.node_dir, line_callback=on_line)

        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
