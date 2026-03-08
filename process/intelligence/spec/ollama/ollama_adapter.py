"""
Ollama Adapter — Implements Provider abstract for local Ollama.

Structure mirrors intelligence.py:
  1. Setup    — check Ollama is running, model is available
  2. Connect  — health check via /api/tags
  3. Complete — POST /api/chat with messages + tools in Ollama format

Translation:
  LLMMessage  → Ollama message format
  ToolSchema  → Ollama tool format (OpenAI-compatible)
  Ollama response → LLMResponse
"""

import json
import os
import sys
import uuid

import httpx

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _project_root)

from process.intelligence.abstract.intelligence import (
    Provider, LLMMessage, ToolSchema, ToolCall, LLMResponse,
)


class OllamaProvider(Provider):

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.base_url = spec.get("base_url", "http://localhost:11434")
        self.model = spec.get("model", "llama3.2:latest")
        self.timeout = spec.get("timeout_seconds", 120)
        self.temperature = spec.get("temperature", 0.3)
        self.max_tokens = spec.get("max_tokens", 2048)

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def _platform_setup(self, user_dir: str) -> bool:
        """Check Ollama is running and model is available."""
        if not self._platform_health():
            print(f"Ollama not reachable at {self.base_url}")
            print("Make sure Ollama is running: ollama serve")
            return False

        # Check if model is pulled
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            models = [m["name"] for m in resp.json().get("models", [])]

            # Match model name (with or without :latest tag)
            model_base = self.model.split(":")[0]
            available = any(model_base in m for m in models)

            if not available:
                print(f"Model '{self.model}' not found locally.")
                print(f"Available models: {', '.join(models) if models else 'none'}")
                print(f"Pull it with: ollama pull {self.model}")
                return False

            print(f"Ollama ready. Model: {self.model}")
            return True

        except Exception as e:
            print(f"Failed to check Ollama models: {e}")
            return False

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def _platform_health(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
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
        """Send messages + tools to Ollama, return LLMResponse."""

        # Translate messages → Ollama format
        ollama_messages = self._translate_messages(messages)

        # Build request
        payload: dict = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }

        # Translate tools → Ollama format (OpenAI-compatible)
        if tools:
            payload["tools"] = self._translate_tools(tools)

        # Call Ollama
        resp = httpx.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        # Translate response → LLMResponse
        return self._translate_response(data)

    # ─────────────────────────────────────────────────────────────
    # TRANSLATORS — between abstract format and Ollama format
    # ─────────────────────────────────────────────────────────────

    def _translate_messages(self, messages: list[LLMMessage]) -> list[dict]:
        """LLMMessage → Ollama message format."""
        result = []
        for msg in messages:
            if msg.role == "tool_result":
                # Ollama expects tool results as role "tool"
                result.append({
                    "role": "tool",
                    "content": msg.content,
                })
            else:
                result.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        return result

    def _translate_tools(self, tools: list[ToolSchema]) -> list[dict]:
        """ToolSchema → Ollama tool format (OpenAI-compatible)."""
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
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return result

    def _translate_response(self, data: dict) -> LLMResponse:
        """Ollama response → LLMResponse."""
        message = data.get("message", {})
        text = message.get("content", "")

        # Parse tool calls if present
        tool_calls = []
        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            tool_calls.append(ToolCall(
                id=str(uuid.uuid4())[:8],  # Ollama doesn't assign IDs
                name=func.get("name", ""),
                arguments=args,
            ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
            model=data.get("model", self.model),
        )
