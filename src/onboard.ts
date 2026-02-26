import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import readline from "readline";
import { PATHS, ensureDirectories } from "../utils/paths.js";
import {
  DEFAULT_APP_CONFIG,
  DEFAULT_MODELS_CONFIG,
  loadAppConfig,
  saveAppConfig,
  saveModelsConfig,
  type AppConfig,
  type ModelsConfig,
} from "../utils/config.js";
import { initDatabase } from "../store/database.js";
import { setupGmailAuth, fetchEmails } from "../input/gmail.js";
import { buildOrganicFilter, classifyEmails } from "../input/filter.js";
import { setupWhatsApp, sendBriefing } from "../output/whatsapp.js";
import { generateBriefing } from "../process/intelligence.js";
import * as db from "../store/database.js";
import logger from "../utils/logger.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// ─── Interactive Input ──────────────────────────────────────────────

function createRl(): readline.Interface {
  return readline.createInterface({ input: process.stdin, output: process.stdout });
}

function ask(rl: readline.Interface, question: string): Promise<string> {
  return new Promise((resolve) => {
    rl.question(question, (answer) => resolve(answer.trim()));
  });
}

function askChoice(rl: readline.Interface, question: string, options: string[]): Promise<number> {
  return new Promise(async (resolve) => {
    console.log(`\n${question}`);
    options.forEach((opt, i) => console.log(`  ${i + 1}. ${opt}`));

    const answer = await ask(rl, `Choose (1-${options.length}): `);
    const choice = parseInt(answer);
    if (choice >= 1 && choice <= options.length) {
      resolve(choice - 1);
    } else {
      console.log("Invalid choice. Using default (1).");
      resolve(0);
    }
  });
}

// ─── Copy Default Files ─────────────────────────────────────────────

function copyDefaultFiles(): void {
  // Resolve defaults directory relative to this source file
  // In dev: src/onboard.ts → ../../defaults/
  // In dist: dist/onboard.js → ../../defaults/
  const defaultsDir = path.resolve(__dirname, "..", "..", "defaults");

  // Copy default rules
  const rulesSource = path.join(defaultsDir, "rules", "default.json");
  if (fs.existsSync(rulesSource) && !fs.existsSync(PATHS.rulesDefault)) {
    fs.copyFileSync(rulesSource, PATHS.rulesDefault);
  }

  // Copy prompt templates
  const promptFiles = [
    { src: "classify-senders.md", dest: PATHS.promptClassifySenders },
    { src: "extract-keywords.md", dest: PATHS.promptExtractKeywords },
    { src: "briefing.md", dest: PATHS.promptBriefing },
  ];

  for (const { src, dest } of promptFiles) {
    const source = path.join(defaultsDir, "prompts", src);
    if (fs.existsSync(source) && !fs.existsSync(dest)) {
      fs.copyFileSync(source, dest);
    }
  }
}

// ─── Main Onboarding ────────────────────────────────────────────────

export async function runOnboarding(): Promise<void> {
  const rl = createRl();

  console.log("\n╔══════════════════════════════════════════════╗");
  console.log("║        🦅 LedgerClaw — First Time Setup      ║");
  console.log("║   Local-first personal finance intelligence   ║");
  console.log("╚══════════════════════════════════════════════╝\n");

  try {
    // ─── Step 1: Create directories ───────────────────────
    console.log("Step 1/8: Creating directory structure...");
    ensureDirectories();
    logger.info("Directories created at ~/.ledgerclaw/");

    // ─── Step 2: Copy default files ───────────────────────
    console.log("Step 2/8: Setting up default configuration...");
    copyDefaultFiles();

    // Write .gitignore
    fs.writeFileSync(
      PATHS.gitignore,
      `.env
input/gmail/token.json
input/rules/user.json
input/store/ledger.db
output/whatsapp/auth/
output/logs/
`
    );

    // ─── Step 3: Choose LLM provider ─────────────────────
    console.log("\nStep 3/8: Choose your LLM provider");
    const providerIdx = await askChoice(rl, "Which LLM do you want to use?", [
      "Ollama (local, private, free) — recommended",
      "Anthropic Claude (cloud, needs API key)",
      "OpenAI GPT (cloud, needs API key)",
    ]);

    const providers: ModelsConfig["provider"][] = ["ollama", "anthropic", "openai"];
    const modelsConfig: ModelsConfig = { ...DEFAULT_MODELS_CONFIG };
    modelsConfig.provider = providers[providerIdx];

    if (providerIdx === 0) {
      // Ollama
      const modelName = await ask(
        rl,
        `Ollama model name (default: ${modelsConfig.ollama.model}): `
      );
      if (modelName) modelsConfig.ollama.model = modelName;

      const baseUrl = await ask(
        rl,
        `Ollama URL (default: ${modelsConfig.ollama.baseUrl}): `
      );
      if (baseUrl) modelsConfig.ollama.baseUrl = baseUrl;
    } else if (providerIdx === 1) {
      // Anthropic
      const apiKey = await ask(rl, "Anthropic API key: ");
      if (apiKey) {
        fs.writeFileSync(PATHS.env, `ANTHROPIC_API_KEY=${apiKey}\n`);
      }
    } else {
      // OpenAI
      const apiKey = await ask(rl, "OpenAI API key: ");
      if (apiKey) {
        fs.writeFileSync(PATHS.env, `OPENAI_API_KEY=${apiKey}\n`);
      }
    }

    saveModelsConfig(modelsConfig);
    logger.info(`LLM provider set to: ${modelsConfig.provider}`);

    // ─── Step 4: Configure schedule ───────────────────────
    console.log("\nStep 4/8: Configure your briefing schedule");
    const appConfig: AppConfig = { ...DEFAULT_APP_CONFIG };

    const timeInput = await ask(rl, "Daily briefing time in 24h format (default: 18:00): ");
    if (timeInput && /^\d{1,2}:\d{2}$/.test(timeInput)) {
      const [hour, minute] = timeInput.split(":").map(Number);
      appConfig.schedule.dailyBriefing = `${minute} ${hour} * * *`;
    }

    const tzInput = await ask(
      rl,
      `Timezone (default: ${appConfig.schedule.timezone}): `
    );
    if (tzInput) appConfig.schedule.timezone = tzInput;

    saveAppConfig(appConfig);
    logger.info(
      `Schedule: ${appConfig.schedule.dailyBriefing} (${appConfig.schedule.timezone})`
    );

    // ─── Step 5: Initialize database ─────────────────────
    console.log("\nStep 5/8: Initializing database...");
    initDatabase();

    // ─── Step 6: Setup Gmail ─────────────────────────────
    console.log("\nStep 6/8: Gmail setup");
    console.log("────────────────────────────────────────────");
    console.log("Before continuing, you need a Google Cloud project:");
    console.log("1. Go to https://console.cloud.google.com");
    console.log("2. Create a new project (name: LedgerClaw)");
    console.log("3. Enable the Gmail API");
    console.log("4. Create OAuth2 credentials (Desktop Application)");
    console.log(`5. Download the JSON file and save it as:`);
    console.log(`   ${PATHS.gmailCredentials}`);
    console.log("────────────────────────────────────────────");

    await ask(rl, "\nPress Enter when credentials.json is in place...");

    await setupGmailAuth();

    // ─── Step 6b: Initial email fetch ────────────────────
    console.log("\nFetching your recent emails (this may take a few minutes)...");
    const fetchResult = await fetchEmails();
    const totalEmails = db.getEmailCount();
    logger.info(`Total emails in database: ${totalEmails}`);

    // ─── Step 7: Build organic filter ────────────────────
    console.log("\nStep 7/8: Building your personalized finance filter...");
    console.log("(Analyzing your email senders and extracting financial patterns)\n");
    const filterTokens = await buildOrganicFilter();
    logger.info(`Filter built using ${filterTokens} tokens`);

    // Classify all fetched emails with the new filter
    console.log("Classifying your emails...");
    const unclassified = db.getUnclassifiedEmailIds();
    const classifyResult = classifyEmails(unclassified);
    logger.info(
      `Classification complete: ${classifyResult.financialCount} financial out of ${classifyResult.total} total`
    );

    // ─── Step 8: Setup WhatsApp ──────────────────────────
    console.log("\nStep 8/8: WhatsApp setup");
    await setupWhatsApp();

    // ─── First briefing ─────────────────────────────────
    console.log("\n🎉 Setup complete! Generating your first briefing...\n");

    const config = loadAppConfig();
    const periodStart = Date.now() - config.gmail.initialFetchDays * 86400000;
    const financialIds = db.getFinancialEmailIdsSince(periodStart);

    if (financialIds.length > 0) {
      const briefingResult = await generateBriefing(financialIds, periodStart, Date.now());

      try {
        await sendBriefing(briefingResult.content);
        db.markBriefingDelivered(briefingResult.briefingId, config.delivery.channel);
        console.log("\n✅ Your first finance briefing has been sent to WhatsApp!");
      } catch (err: any) {
        console.log(`\n⚠️  Briefing generated but delivery failed: ${err.message}`);
        console.log("The briefing is saved in the database.");
      }

      // Create a successful pipeline run
      const runId = db.createPipelineRun("manual");
      db.updatePipelineRun(runId, {
        status: "success",
        finished_at: Date.now(),
        period_start: periodStart,
        period_end: Date.now(),
        emails_fetched: fetchResult.count,
        emails_classified: classifyResult.total,
        emails_financial: classifyResult.financialCount,
        tokens_used: filterTokens + briefingResult.tokensUsed,
        briefing_id: briefingResult.briefingId,
      });
    } else {
      console.log("\nNo financial emails found. Your first briefing will be generated tomorrow.");
    }

    console.log("\n────────────────────────────────────────────");
    console.log("LedgerClaw is ready! Start the scheduler with:");
    console.log("  npm start       (production)");
    console.log("  npm run dev     (development)");
    console.log("────────────────────────────────────────────\n");
  } finally {
    rl.close();
  }
}
