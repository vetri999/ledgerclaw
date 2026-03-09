"""
Ollama Adapter — Implements Provider abstract for local Ollama.

Structure mirrors intelligence.py:
  1. Setup    — install Ollama → start server → pull model → verify ready
  2. Connect  — health check via /api/tags
  3. Complete — POST /api/chat with messages + tools in Ollama format

Translation (used inside Complete):
  LLMMessage  → Ollama message format
  ToolSchema  → Ollama tool format (OpenAI-compatible)
  Ollama response → LLMResponse
"""

import json
import os
import platform
import subprocess
import sys
import time
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
        # All values must exist in ollama.yaml — no silent Python fallbacks.
        # If a key is missing, raise immediately with a clear message so the
        # user knows exactly which YAML key to add, rather than silently using
        # a hardcoded value that may not match their environment.
        required_keys = ["base_url", "model", "timeout_seconds", "temperature", "max_tokens"]
        for key in required_keys:
            if key not in spec:
                raise KeyError(
                    f"ollama.yaml is missing required key: '{key}'. "
                    f"Add it to process/intelligence/spec/ollama/ollama.yaml"
                )

        self.base_url    = spec["base_url"]           # e.g. http://localhost:11434
        self.model       = spec["model"]              # e.g. qwen2.5:4b
        self.timeout     = spec["timeout_seconds"]    # seconds before request times out
        self.temperature = spec["temperature"]        # 0.0 = deterministic, 1.0 = creative
        self.max_tokens  = spec["max_tokens"]         # max tokens in the response

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # Broken into four small units, each doing exactly one thing.
    # _platform_setup orchestrates them in order.
    # ─────────────────────────────────────────────────────────────

    def _platform_setup(self, user_dir: str) -> bool:
        """
        Full Ollama setup from scratch. Steps:
          1. Install Ollama if missing
          2. Start server if not running
          3. Pull model if not downloaded
          4. Verify everything is ready
        Safe to re-run — each step skips itself if already done.
        """
        if not self._setup_step1_install():
            return False
        if not self._setup_step2_start_server():
            return False
        if not self._setup_step3_pull_model():
            return False
        if not self._setup_step4_verify():
            return False
        return True

    def _setup_step1_install(self) -> bool:
        """
        Step 1 — Install Ollama if it is not already installed.
        Checks by running `ollama --version`. If that fails, runs the
        official install script (macOS/Linux) or prints Windows instructions.
        """
        # Check if already installed.
        # FileNotFoundError means the binary is not on PATH at all — not a
        # failed run, but a missing executable. Treat both cases as not installed.
        try:
            result = subprocess.run(
                ["ollama", "--version"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"Ollama already installed: {result.stdout.strip()}")
                return True
        except FileNotFoundError:
            pass  # binary not found — fall through to install

        print("Ollama not found. Installing...")

        os_name = platform.system()

        if os_name in ("Darwin", "Linux"):
            # Official one-line install script for macOS and Linux
            install = subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True,
            )
            if install.returncode != 0:
                print("Ollama install failed. Install manually from https://ollama.com")
                return False

        elif os_name == "Windows":
            print("Automatic install is not supported on Windows.")
            print("Download and run the installer from: https://ollama.com/download/windows")
            return False

        else:
            print(f"Unknown OS: {os_name}. Install Ollama manually from https://ollama.com")
            return False

        # Confirm install succeeded
        try:
            check = subprocess.run(["ollama", "--version"], capture_output=True, text=True)
            if check.returncode != 0:
                raise FileNotFoundError
        except FileNotFoundError:
            print("Ollama installed but `ollama` command not found. Restart your terminal.")
            return False

        print(f"Ollama installed: {check.stdout.strip()}")
        return True

    def _setup_step2_start_server(self) -> bool:
        """
        Step 2 — Start the Ollama server if it is not already running.
        Checks by hitting /api/tags. If unreachable, starts `ollama serve`
        as a background process and waits up to 10 seconds for it to be ready.
        """
        if self._platform_health():
            print("Ollama server already running.")
            return True

        print("Starting Ollama server...")

        # Start server detached so it outlives this script
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,  # suppress server log output
            stderr=subprocess.DEVNULL,
            start_new_session=True,     # detach from this process group
        )

        # Wait up to 10 seconds for the server to become ready
        for attempt in range(10):
            time.sleep(1)
            if self._platform_health():
                print("Ollama server started.")
                return True
            print(f"  Waiting for server... ({attempt + 1}/10)")

        print("Ollama server did not start in time. Try running `ollama serve` manually.")
        return False

    def _setup_step3_pull_model(self) -> bool:
        """
        Step 3 — Pull the configured model if it is not already downloaded.
        Fetches the local model list from /api/tags and only pulls if the
        model is missing. Pull streams progress live to the terminal.
        """
        # Fetch list of already-downloaded models
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            installed_models = [m["name"] for m in resp.json().get("models", [])]
        except Exception as e:
            print(f"Could not fetch model list: {e}")
            return False

        # Match on base name — e.g. "qwen2.5" matches "qwen2.5:4b"
        model_base = self.model.split(":")[0]
        already_installed = any(model_base in m for m in installed_models)

        if already_installed:
            print(f"Model '{self.model}' already downloaded.")
            return True

        # Model not found — pull it now
        print(f"Pulling model '{self.model}'... (this may take a few minutes)")

        pull = subprocess.run(["ollama", "pull", self.model])

        if pull.returncode != 0:
            print(f"Failed to pull model '{self.model}'.")
            print(f"Try manually: ollama pull {self.model}")
            return False

        print(f"Model '{self.model}' ready.")
        return True

    def _setup_step4_verify(self) -> bool:
        """
        Step 4 — Final verification that server is up and model is listed.
        This is the same check _platform_health uses at runtime, so passing
        here guarantees complete() will work immediately after setup.
        """
        if not self._platform_health():
            print("Ollama server is not reachable after setup. Something went wrong.")
            return False

        # Confirm the model appears in the installed list
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            installed_models = [m["name"] for m in resp.json().get("models", [])]
            model_base = self.model.split(":")[0]
            if not any(model_base in m for m in installed_models):
                print(f"Model '{self.model}' not found after pull. Setup incomplete.")
                return False
        except Exception as e:
            print(f"Verification failed: {e}")
            return False

        print(f"Ollama ready. Model: {self.model}")
        return True

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def _platform_health(self) -> bool:
        """
        Quick liveness check — called frequently by IntelligenceManager
        during provider selection and fallback. Hits /api/tags which is
        Ollama's lightest endpoint. Returns False on any error.
        """
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
        """
        Send messages + tools to Ollama, return LLMResponse.
        Translates in:  LLMMessage → Ollama format, ToolSchema → Ollama format
        Translates out: Ollama response → LLMResponse
        """
        ollama_messages = self._translate_messages(messages)   # convert to Ollama format

        payload: dict = {
            "model":    self.model,
            "messages": ollama_messages,
            "stream":   False,                                  # get full response at once
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }

        if tools:
            payload["tools"] = self._translate_tools(tools)   # attach tools if provided

        resp = httpx.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()                                # raise on 4xx/5xx

        return self._translate_response(resp.json())           # convert back to LLMResponse

    # ─────────────────────────────────────────────────────────────
    # TRANSLATORS — between abstract format and Ollama's API format
    # ─────────────────────────────────────────────────────────────

    def _translate_messages(self, messages: list[LLMMessage]) -> list[dict]:
        """
        LLMMessage → Ollama message format.
        Most roles pass through unchanged. "tool_result" becomes "tool"
        because that is what Ollama's API expects for tool call results.
        """
        result = []
        for msg in messages:
            if msg.role == "tool_result":
                result.append({"role": "tool", "content": msg.content})  # rename role
            else:
                result.append({"role": msg.role, "content": msg.content})
        return result

    def _translate_tools(self, tools: list[ToolSchema]) -> list[dict]:
        """
        ToolSchema → Ollama tool format (OpenAI-compatible JSON Schema).
        Builds the nested "function" wrapper Ollama expects.
        Parameters marked required=True are added to the required array.
        """
        result = []
        for tool in tools:
            properties = {}
            required   = []
            for param_name, param_info in tool.parameters.items():
                properties[param_name] = {
                    "type":        param_info.get("type", "string"),
                    "description": param_info.get("description", ""),
                }
                if param_info.get("required", True):
                    required.append(param_name)

            result.append({
                "type": "function",
                "function": {
                    "name":        tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type":       "object",
                        "properties": properties,
                        "required":   required,
                    },
                },
            })
        return result

    def _translate_response(self, data: dict) -> LLMResponse:
        """
        Ollama response → LLMResponse.
        Reads message.content for text replies.
        Reads message.tool_calls for tool invocations (generates IDs since
        Ollama does not assign them). Sums prompt + eval tokens for usage.
        """
        message    = data.get("message", {})
        text       = message.get("content", "")
        tool_calls = []

        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            args = func.get("arguments", {})
            if isinstance(args, str):              # Ollama sometimes returns args as a string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            tool_calls.append(ToolCall(
                id=str(uuid.uuid4())[:8],          # generate ID — Ollama does not provide one
                name=func.get("name", ""),
                arguments=args,
            ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            tokens_used=(
                data.get("eval_count", 0) +        # output tokens
                data.get("prompt_eval_count", 0)   # input tokens
            ),
            model=data.get("model", self.model),
        )