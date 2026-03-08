"""
Intelligence Abstract — Base class for all LLM providers.

Structure:
  1. Setup      — choose provider, configure model, save preferences
  2. Connect    — verify provider health, establish connection
  3. Complete   — send prompt + tools, receive response + tool calls
  4. Manage     — provider selection by priority, automatic fallback

Scaling: To add a new provider (OpenAI, Gemini, local GGUF, etc.):
  1. Subclass this
  2. Implement the 3 _platform methods (marked with "IMPLEMENT THIS")
  3. Provider selection + fallback is handled by IntelligenceManager

Data structures:
  LLMMessage   — one message in the conversation (role + content)
  ToolSchema   — tool definition the LLM can call (name, description, params)
  ToolCall     — tool call requested by the LLM (name, arguments)
  LLMResponse  — LLM's response (text + tool_calls + usage metadata)
"""

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────

@dataclass
class LLMMessage:
    """One message in the conversation."""
    role: str          # "system", "user", "assistant", "tool_result"
    content: str       # message text
    tool_call_id: str = ""   # if role is "tool_result", which tool call this answers


@dataclass
class ToolSchema:
    """Tool definition — tells the LLM what it can call."""
    name: str                     # e.g. "fetch_source"
    description: str              # what this tool does
    parameters: dict = field(default_factory=dict)   # param name → {type, description}


@dataclass
class ToolCall:
    """Tool call requested by the LLM."""
    id: str            # unique ID for this call (provider-assigned)
    name: str          # which tool to call
    arguments: dict    # arguments to pass


@dataclass
class LLMResponse:
    """LLM's response — may contain text, tool calls, or both."""
    text: str = ""                                    # final response text
    tool_calls: list[ToolCall] = field(default_factory=list)  # requested tool invocations
    tokens_used: int = 0                              # total tokens consumed
    model: str = ""                                   # which model produced this
    provider: str = ""                                # which provider was used


# ─────────────────────────────────────────────────────────────────
# PROVIDER BASE CLASS
# ─────────────────────────────────────────────────────────────────

class Provider(ABC):
    """Base class for a single LLM provider (Ollama, Anthropic, etc.)"""

    def __init__(self, spec: dict):
        """
        spec — parsed YAML config (model, timeout, base_url, priority, etc.)
        """
        self.spec = spec
        self.name = spec.get("name", "unknown")
        self.priority = spec.get("priority", 99)  # lower = higher priority

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    def _platform_setup(self, user_dir: str) -> bool:
        """IMPLEMENT THIS — Validate provider config (API key exists, model available, etc.)"""
        pass

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    def _platform_health(self) -> bool:
        """IMPLEMENT THIS — Check if provider is reachable and ready."""
        pass

    # ─────────────────────────────────────────────────────────────
    # 3. COMPLETE
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    def _platform_complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSchema] = None,
    ) -> LLMResponse:
        """
        IMPLEMENT THIS — Send messages + tools to LLM, return response.

        Each provider translates:
          - LLMMessage → provider's message format
          - ToolSchema → provider's tool format
          - Provider's response → LLMResponse
        """
        pass


# ─────────────────────────────────────────────────────────────────
# INTELLIGENCE MANAGER
# ─────────────────────────────────────────────────────────────────
# Manages multiple providers. Handles selection, fallback, setup.

class IntelligenceManager:
    """
    Central intelligence hub. Manages all LLM providers.

    Usage:
      manager = IntelligenceManager(user_dir)
      manager.load_providers([ollama_spec, anthropic_spec], [OllamaProvider, AnthropicProvider])
      manager.setup()       # interactive — user picks default provider
      manager.connect()     # health check all providers
      response = manager.complete(messages, tools)  # auto-selects best provider
    """

    def __init__(self, user_dir: str):
        self.user_dir = os.path.abspath(user_dir)
        self.config_path = os.path.join(self.user_dir, "config.json")
        self.providers: list[Provider] = []

    # ─────────────────────────────────────────────────────────────
    # 1. SETUP
    # ─────────────────────────────────────────────────────────────

    def load_config(self) -> Optional[dict]:
        """Read user's intelligence preferences."""
        if not os.path.exists(self.config_path):
            return None
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def save_config(self, data: dict) -> None:
        """Save user's intelligence preferences."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

    def load_providers(self, specs: list[dict], provider_classes: list[type]) -> None:
        """Instantiate providers from specs + classes. Sort by priority."""
        self.providers = []
        for spec, cls in zip(specs, provider_classes):
            self.providers.append(cls(spec))
        # Sort: lower priority number = tried first
        self.providers.sort(key=lambda p: p.priority)

    def setup(self) -> bool:
        """
        Interactive setup:
          1. Show available providers
          2. User picks default (local or cloud)
          3. Validate the chosen provider's config
          4. Save preferences
        """
        if not self.providers:
            print("No providers loaded. Load providers first.")
            return False

        # Check if already configured
        config = self.load_config()
        if config and config.get("default_provider"):
            print(f"Intelligence already configured. Default: {config['default_provider']}")
            # Still validate
            return self._validate_providers()

        print("\nAvailable LLM providers:")
        for i, p in enumerate(self.providers):
            label = "local" if p.spec.get("local", False) else "cloud"
            model = p.spec.get("model", "unknown")
            print(f"  {i + 1}. {p.name} ({label}) — model: {model}")

        print("")
        choice = input("Choose default provider (number): ").strip()

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(self.providers):
                raise ValueError()
        except ValueError:
            print("Invalid choice.")
            return False

        chosen = self.providers[idx]

        # Validate chosen provider
        if not chosen._platform_setup(self.user_dir):
            return False

        # If cloud provider, prompt for API key
        api_key_env = chosen.spec.get("api_key_env")
        if api_key_env:
            env_path = os.path.join(self.user_dir, ".env")
            existing_key = self._read_env_key(env_path, api_key_env)

            if not existing_key:
                key = input(f"Enter your {api_key_env}: ").strip()
                if not key:
                    print("API key required for cloud provider.")
                    return False
                self._write_env_key(env_path, api_key_env, key)
                print(f"API key saved to {env_path}")

        # Save config — set chosen as priority 1
        self.save_config({
            "default_provider": chosen.name,
            "model": chosen.spec.get("model", ""),
        })

        # Re-sort: put chosen provider first
        self.providers.sort(key=lambda p: 0 if p.name == chosen.name else p.priority)

        print(f"Intelligence configured. Default: {chosen.name}")
        return True

    def _validate_providers(self) -> bool:
        """Check at least one provider can be reached."""
        for p in self.providers:
            if p._platform_health():
                return True
        print("Warning: no providers are reachable.")
        return False

    # ─────────────────────────────────────────────────────────────
    # 2. CONNECT
    # ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Health check all providers. Returns True if at least one is healthy."""
        healthy = []
        for p in self.providers:
            if p._platform_health():
                healthy.append(p.name)

        if healthy:
            print(f"Providers available: {', '.join(healthy)}")
            return True

        print("No providers available.")
        return False

    def _select_provider(self) -> Optional[Provider]:
        """Pick highest-priority healthy provider."""
        # Load user config to respect their default choice
        config = self.load_config()
        default_name = config.get("default_provider") if config else None

        # Try default first
        if default_name:
            for p in self.providers:
                if p.name == default_name and p._platform_health():
                    return p

        # Fallback to any healthy provider by priority
        for p in self.providers:
            if p._platform_health():
                return p

        return None

    # ─────────────────────────────────────────────────────────────
    # 3. COMPLETE
    # ─────────────────────────────────────────────────────────────

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolSchema] = None,
    ) -> Optional[LLMResponse]:
        """
        Send prompt + tools to the best available provider.
        Automatic fallback: if default fails, tries next by priority.
        Returns LLMResponse or None if all providers fail.
        """
        # Try each provider in priority order
        errors = []
        for provider in self.providers:
            if not provider._platform_health():
                continue

            try:
                response = provider._platform_complete(messages, tools)
                response.provider = provider.name
                return response
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                print(f"Provider {provider.name} failed: {e}")
                continue

        if errors:
            print(f"All providers failed: {'; '.join(errors)}")
        else:
            print("No healthy providers available.")
        return None

    # ─────────────────────────────────────────────────────────────
    # HELPERS — .env file management
    # ─────────────────────────────────────────────────────────────

    def _read_env_key(self, env_path: str, key_name: str) -> Optional[str]:
        """Read a specific key from .env file."""
        if not os.path.exists(env_path):
            return None
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key_name}="):
                    return line.split("=", 1)[1].strip()
        return None

    def _write_env_key(self, env_path: str, key_name: str, value: str) -> None:
        """Write/update a key in .env file."""
        lines = []
        found = False

        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if line.strip().startswith(f"{key_name}="):
                        lines.append(f"{key_name}={value}\n")
                        found = True
                    else:
                        lines.append(line)

        if not found:
            lines.append(f"{key_name}={value}\n")

        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.writelines(lines)
