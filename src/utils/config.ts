import fs from "fs";
import { PATHS } from "./paths.js";
import dotenv from "dotenv";

// ─── Type Definitions ───────────────────────────────────────────────

export interface AppConfig {
  version: number;
  schedule: {
    dailyBriefing: string;
    timezone: string;
  };
  gmail: {
    initialFetchDays: number;
    maxResultsPerPage: number;
    batchSize: number;
    batchDelayMs: number;
    maxRetries: number;
  };
  delivery: {
    channel: string;
    recipientJid: string | null;
  };
  filter: {
    refreshIntervalDays: number;
  };
}

export interface ModelsConfig {
  provider: "ollama" | "anthropic" | "openai";
  ollama: { baseUrl: string; model: string };
  anthropic: { model: string };
  openai: { model: string };
  fallback: "ollama" | "anthropic" | "openai" | null;
  requestTimeoutMs: number;
}

export interface DefaultRules {
  version: number;
  senders: { financial: string[]; ignore: string[] };
  keywords: { subject: string[]; body: string[] };
  categories: Record<string, { senderHints: string[]; keywordHints: string[] }>;
}

export interface UserRules {
  version: number;
  generated_at: string;
  last_refreshed_at: string;
  senders: { financial: string[]; ignore: string[]; pending_review: string[] };
  keywords: { subject: string[]; body: string[] };
}

// ─── Defaults ───────────────────────────────────────────────────────

export const DEFAULT_APP_CONFIG: AppConfig = {
  version: 1,
  schedule: { dailyBriefing: "0 18 * * *", timezone: "Asia/Kolkata" },
  gmail: {
    initialFetchDays: 90,
    maxResultsPerPage: 100,
    batchSize: 25,
    batchDelayMs: 1000,
    maxRetries: 3,
  },
  delivery: { channel: "whatsapp", recipientJid: null },
  filter: { refreshIntervalDays: 7 },
};

export const DEFAULT_MODELS_CONFIG: ModelsConfig = {
  provider: "ollama",
  ollama: { baseUrl: "http://localhost:11434", model: "llama3.2:7b" },
  anthropic: { model: "claude-sonnet-4-5-20250514" },
  openai: { model: "gpt-4o" },
  fallback: null,
  requestTimeoutMs: 120000,
};

// ─── Load / Save ────────────────────────────────────────────────────

export function loadAppConfig(): AppConfig {
  if (!fs.existsSync(PATHS.config)) return { ...DEFAULT_APP_CONFIG };
  const raw = JSON.parse(fs.readFileSync(PATHS.config, "utf-8"));
  return {
    ...DEFAULT_APP_CONFIG,
    ...raw,
    schedule: { ...DEFAULT_APP_CONFIG.schedule, ...raw.schedule },
    gmail: { ...DEFAULT_APP_CONFIG.gmail, ...raw.gmail },
    delivery: { ...DEFAULT_APP_CONFIG.delivery, ...raw.delivery },
    filter: { ...DEFAULT_APP_CONFIG.filter, ...raw.filter },
  };
}

export function saveAppConfig(config: AppConfig): void {
  fs.writeFileSync(PATHS.config, JSON.stringify(config, null, 2));
}

export function loadModelsConfig(): ModelsConfig {
  if (!fs.existsSync(PATHS.modelsConfig)) return { ...DEFAULT_MODELS_CONFIG };
  const raw = JSON.parse(fs.readFileSync(PATHS.modelsConfig, "utf-8"));
  return {
    ...DEFAULT_MODELS_CONFIG,
    ...raw,
    ollama: { ...DEFAULT_MODELS_CONFIG.ollama, ...raw.ollama },
    anthropic: { ...DEFAULT_MODELS_CONFIG.anthropic, ...raw.anthropic },
    openai: { ...DEFAULT_MODELS_CONFIG.openai, ...raw.openai },
  };
}

export function saveModelsConfig(config: ModelsConfig): void {
  fs.writeFileSync(PATHS.modelsConfig, JSON.stringify(config, null, 2));
}

export function loadDefaultRules(): DefaultRules {
  return JSON.parse(fs.readFileSync(PATHS.rulesDefault, "utf-8"));
}

export function loadUserRules(): UserRules | null {
  if (!fs.existsSync(PATHS.rulesUser)) return null;
  return JSON.parse(fs.readFileSync(PATHS.rulesUser, "utf-8"));
}

export function saveUserRules(rules: UserRules): void {
  fs.writeFileSync(PATHS.rulesUser, JSON.stringify(rules, null, 2));
}

export function loadEnv(): void {
  if (fs.existsSync(PATHS.env)) {
    dotenv.config({ path: PATHS.env });
  }
}
