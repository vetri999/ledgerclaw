import path from "path";
import os from "os";
import fs from "fs";

const ROOT = path.join(os.homedir(), ".ledgerclaw");

export const PATHS = {
  root: ROOT,

  // Top-level
  config: path.join(ROOT, "config.json"),
  env: path.join(ROOT, ".env"),
  gitignore: path.join(ROOT, ".gitignore"),

  // Input
  input: path.join(ROOT, "input"),
  gmailDir: path.join(ROOT, "input", "gmail"),
  gmailCredentials: path.join(ROOT, "input", "gmail", "credentials.json"),
  gmailToken: path.join(ROOT, "input", "gmail", "token.json"),
  rulesDir: path.join(ROOT, "input", "rules"),
  rulesDefault: path.join(ROOT, "input", "rules", "default.json"),
  rulesUser: path.join(ROOT, "input", "rules", "user.json"),
  storeDir: path.join(ROOT, "input", "store"),
  database: path.join(ROOT, "input", "store", "ledger.db"),

  // Process
  process: path.join(ROOT, "process"),
  promptsDir: path.join(ROOT, "process", "prompts"),
  promptClassifySenders: path.join(ROOT, "process", "prompts", "classify-senders.md"),
  promptExtractKeywords: path.join(ROOT, "process", "prompts", "extract-keywords.md"),
  promptBriefing: path.join(ROOT, "process", "prompts", "briefing.md"),
  modelsDir: path.join(ROOT, "process", "models"),
  modelsConfig: path.join(ROOT, "process", "models", "config.json"),

  // Output
  output: path.join(ROOT, "output"),
  whatsappDir: path.join(ROOT, "output", "whatsapp"),
  whatsappAuth: path.join(ROOT, "output", "whatsapp", "auth"),
  logsDir: path.join(ROOT, "output", "logs"),
  pipelineLog: path.join(ROOT, "output", "logs", "pipeline.log"),
};

export function ensureDirectories(): void {
  const dirs = [
    PATHS.gmailDir,
    PATHS.rulesDir,
    PATHS.storeDir,
    PATHS.promptsDir,
    PATHS.modelsDir,
    PATHS.whatsappAuth,
    PATHS.logsDir,
  ];
  for (const dir of dirs) {
    fs.mkdirSync(dir, { recursive: true });
  }
}
