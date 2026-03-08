"""
Anthropic Adapter — Implements Provider abstract for Claude API.

Structure mirrors intelligence.py:
  1. Setup    — validate API key exists
  2. Connect  — test API call to verify key
  3. Complete — messages.create() with tools in Anthropic format

Translation:
  LLMMessage  → Anthropic message format (system separate from messages)
  ToolSchema  → Anthropic tool format (input_schema)
  Anthropic response → LLMResponse
"""

import os
import sys

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _project_root)

from process.intelligence.abstract.intelligence import (
    Provider, LLMMessage, ToolSchema, ToolCall, LLMResponse,
)

import anthropic


class AnthropicProvider(Provider):

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.model = spec.get("model", "claude-sonnet-4-5-20250514")
        self.api_key_env = spec.get("api_key_env", "ANTHROPIC_API_KEY")
        self.timeout = spec.get("timeout_seconds", 60)
        self.temperature = spec.get("temperature", 0.3)
        self.max_tokens = spec.get("max_tokens", 2048)

        # Client — created during health/complete (needs API key)
        self._client = None

    def _get_client(self) -> anthropic.Anthropic:
        """Get or create Anthropic client. Reads API key from env or .env file."""
        if self._client:
            return self._client

        api_key = os.environ.get(self.api_key_env)

        # Also check user's .env file
        if not api_key:
            env_path = os.path.join(_project_root, "process", "intelligence", "user", ".env")
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(f"{self.api_key_env}="):
                            api_key = line.split("=", 1)[1].strip()
                            break

        if not api_key:
            raise ValueError(f"{self.api_key_env} not found in environment or user/.env")

        self._client = anthropic.Anthropic(api_key=api_key, timeout=self.timeout)
        return self._client

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def _platform_setup(self, user_dir: str) -> bool:
        """Validate that API key is available."""
        try:
            self._get_client()
            print(f"Anthropic ready. Model: {self.model}")
            return True
        except ValueError as e:
            print(f"Anthropic setup: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def _platform_health(self) -> bool:
        """Check if API key is valid with a minimal call."""
        try:
            client = self._get_client()
            # Minimal call to verify key — count tokens is cheap
            client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "test"}],
            )
            return True
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────
    # 3. COMPLETE
    # ─────────────────────────────────────────────────────────────

    def _platform_complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSchema] = None,
    ) -> LLMResponse:
        """Send messages + tools to Anthropic, return LLMResponse."""
        client = self._get_client()

        # Separate system message from conversation
        system_text, conv_messages = self._translate_messages(messages)

        # Build request
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": conv_messages,
        }

        if system_text:
            kwargs["system"] = system_text

        if tools:
            kwargs["tools"] = self._translate_tools(tools)

        # Call Anthropic
        response = client.messages.create(**kwargs)

        # Translate response → LLMResponse
        return self._translate_response(response)

    # ─────────────────────────────────────────────────────────────
    # TRANSLATORS — between abstract format and Anthropic format
    # ─────────────────────────────────────────────────────────────

    def _translate_messages(self, messages: list[LLMMessage]) -> tuple[str, list[dict]]:
        """
        LLMMessage → Anthropic format.
        Returns (system_text, conversation_messages).
        Anthropic requires system message separate from messages array.
        """
        system_text = ""
        conv = []

        for msg in messages:
            if msg.role == "system":
                system_text += msg.content + "\n"

            elif msg.role == "tool_result":
                conv.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content,
                    }],
                })

            else:
                conv.append({
                    "role": msg.role,
                    "content": msg.content,
                })

        return system_text.strip(), conv

    def _translate_tools(self, tools: list[ToolSchema]) -> list[dict]:
        """ToolSchema → Anthropic tool format."""
        result = []
        for tool in tools:
            properties = {}
            required = []
            for param_name, param_info in tool.parameters.items():
                properties[param_name] = {
                    "type": param_info.get("type", "string"),
                    "description": param_info.get("description", ""),
                }
                if param_info.get("required", True):
                    required.append(param_name)

            result.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            })
        return result

    def _translate_response(self, response) -> LLMResponse:
        """Anthropic response → LLMResponse."""
        text = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input if isinstance(block.input, dict) else {},
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            tokens_used=response.usage.input_tokens + response.usage.output_tokens,
            model=response.model,
        )
