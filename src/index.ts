#!/usr/bin/env node

import { ensureDirectories } from "./utils/paths.js";
import { loadEnv } from "./utils/config.js";
import { initDatabase } from "./store/database.js";
import logger from "./utils/logger.js";

async function main(): Promise<void> {
  const command = process.argv[2];

  switch (command) {
    case "onboard":
    case "setup": {
      const { runOnboarding } = await import("./onboard.js");
      await runOnboarding();
      break;
    }

    case "run": {
      // Manual one-off pipeline run
      ensureDirectories();
      loadEnv();
      initDatabase();
      const { runPipeline } = await import("./pipeline.js");
      await runPipeline("manual");
      break;
    }

    default: {
      // Default: start the scheduler daemon
      ensureDirectories();
      loadEnv();
      initDatabase();
      const { startScheduler } = await import("./scheduler.js");
      await startScheduler();
      break;
    }
  }
}

main().catch((error) => {
  logger.error(`Fatal error: ${error.message}`);
  if (error.stack) logger.error(error.stack);
  process.exit(1);
});
