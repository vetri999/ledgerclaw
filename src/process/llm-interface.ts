import { selectProvider, type ProviderInfo } from "./llm-router.js";
import { loadModelsConfig } from "../utils/config.js";
import logger from "../utils/logger.js";

export interface LLMCallOptions {
  prompt: string;
  systemPrompt?: string;
  responseFormat?: "json" | "text";
  temperature?: number;
  maxTokens?: number;
}

export interface LLMResponse {
  text: string;
  tokensUsed: number;
  model: string;
}

// ─── Provider-specific calls ────────────────────────────────────────

async function callOllama(
  provider: ProviderInfo,
  options: LLMCallOptions,
  timeoutMs: number
): Promise<LLMResponse> {
  const body: any = {
    model: provider.model,
    prompt: options.prompt,
    stream: false,
    options: {
      temperature: options.temperature ?? 0.7,
      num_predict: options.maxTokens ?? 1024,
    },
  };

  if (options.responseFormat === "json") {
    body.format = "json";
  }
  if (options.systemPrompt) {
    body.system = options.systemPrompt;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  const res = await fetch(`${provider.baseUrl}/api/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: controller.signal,
  });
  clearTimeout(timeout);

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`Ollama returned ${res.status}: ${errText}`);
  }

  const data = await res.json();
  return {
    text: data.response || "",
    tokensUsed: (data.prompt_eval_count || 0) + (data.eval_count || 0),
    model: data.model || provider.model,
  };
}

async function callAnthropic(
  provider: ProviderInfo,
  options: LLMCallOptions,
  timeoutMs: number
): Promise<LLMResponse> {
  const body: any = {
    model: provider.model,
    max_tokens: options.maxTokens ?? 1024,
    messages: [{ role: "user", content: options.prompt }],
  };
  if (options.systemPrompt) {
    body.system = options.systemPrompt;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "x-api-key": provider.apiKey!,
      "anthropic-version": "2023-06-01",
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
    signal: controller.signal,
  });
  clearTimeout(timeout);

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`Anthropic returned ${res.status}: ${errText}`);
  }

  const data = await res.json();
  const text = (data.content || [])
    .filter((b: any) => b.type === "text")
    .map((b: any) => b.text)
    .join("");
  const tokensUsed = (data.usage?.input_tokens || 0) + (data.usage?.output_tokens || 0);

  return { text, tokensUsed, model: data.model || provider.model };
}

async function callOpenAI(
  provider: ProviderInfo,
  options: LLMCallOptions,
  timeoutMs: number
): Promise<LLMResponse> {
  const messages: any[] = [];
  if (options.systemPrompt) {
    messages.push({ role: "system", content: options.systemPrompt });
  }
  messages.push({ role: "user", content: options.prompt });

  const body: any = {
    model: provider.model,
    messages,
    max_tokens: options.maxTokens ?? 1024,
    temperature: options.temperature ?? 0.7,
  };
  if (options.responseFormat === "json") {
    body.response_format = { type: "json_object" };
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${provider.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal: controller.signal,
  });
  clearTimeout(timeout);

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`OpenAI returned ${res.status}: ${errText}`);
  }

  const data = await res.json();
  const text = data.choices?.[0]?.message?.content || "";
  const tokensUsed = data.usage?.total_tokens || 0;

  return { text, tokensUsed, model: data.model || provider.model };
}

// ─── Unified call with retry ────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function isRetryable(error: any): boolean {
  const msg = String(error?.message || "");
  return (
    msg.includes("429") ||
    msg.includes("500") ||
    msg.includes("502") ||
    msg.includes("503") ||
    msg.includes("abort")
  );
}

export async function callLLM(
  options: LLMCallOptions,
  maxRetries: number = 2
): Promise<LLMResponse> {
  const config = loadModelsConfig();
  const provider = await selectProvider();
  const timeoutMs = config.requestTimeoutMs;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      let response: LLMResponse;

      switch (provider.type) {
        case "ollama":
          response = await callOllama(provider, options, timeoutMs);
          break;
        case "anthropic":
          response = await callAnthropic(provider, options, timeoutMs);
          break;
        case "openai":
          response = await callOpenAI(provider, options, timeoutMs);
          break;
        default:
          throw new Error(`Unknown provider type: ${provider.type}`);
      }

      return response;
    } catch (error: any) {
      if (!isRetryable(error) || attempt === maxRetries) {
        throw error;
      }
      const delayMs = Math.pow(2, attempt) * 1000;
      logger.warn(
        `LLM call failed (${error.message}), retrying in ${delayMs}ms (attempt ${attempt + 1}/${maxRetries})`
      );
      await sleep(delayMs);
    }
  }

  throw new Error("Unreachable");
}
