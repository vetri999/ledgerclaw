import { loadModelsConfig, loadEnv, type ModelsConfig } from "../utils/config.js";
import logger from "../utils/logger.js";

export interface ProviderInfo {
  type: "ollama" | "anthropic" | "openai";
  model: string;
  baseUrl?: string;
  apiKey?: string;
}

async function isOllamaRunning(baseUrl: string): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    const res = await fetch(`${baseUrl}/api/tags`, { signal: controller.signal });
    clearTimeout(timeout);
    return res.ok;
  } catch {
    return false;
  }
}

function resolveProvider(config: ModelsConfig, providerName: string): ProviderInfo {
  switch (providerName) {
    case "ollama":
      return { type: "ollama", model: config.ollama.model, baseUrl: config.ollama.baseUrl };
    case "anthropic":
      return {
        type: "anthropic",
        model: config.anthropic.model,
        apiKey: process.env.ANTHROPIC_API_KEY,
      };
    case "openai":
      return {
        type: "openai",
        model: config.openai.model,
        apiKey: process.env.OPENAI_API_KEY,
      };
    default:
      throw new Error(`Unknown LLM provider: ${providerName}`);
  }
}

export async function selectProvider(): Promise<ProviderInfo> {
  loadEnv();
  const config = loadModelsConfig();
  const primary = config.provider;

  if (primary === "ollama") {
    const running = await isOllamaRunning(config.ollama.baseUrl);
    if (running) {
      return resolveProvider(config, "ollama");
    }
    if (config.fallback) {
      logger.warn(`Ollama not running at ${config.ollama.baseUrl}, falling back to ${config.fallback}`);
      return resolveProvider(config, config.fallback);
    }
    throw new Error(
      `Ollama is not running at ${config.ollama.baseUrl} and no fallback provider configured.`
    );
  }

  // Cloud providers — just return directly
  const provider = resolveProvider(config, primary);

  if ((primary === "anthropic" || primary === "openai") && !provider.apiKey) {
    throw new Error(
      `API key for ${primary} not found. Set it in ~/.ledgerclaw/.env`
    );
  }

  return provider;
}
