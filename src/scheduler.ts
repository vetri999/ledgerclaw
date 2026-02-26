import cron from "node-cron";
import { loadAppConfig } from "../utils/config.js";
import { runPipeline } from "../pipeline.js";
import { getLastSuccessfulRun } from "../store/database.js";
import logger from "../utils/logger.js";

/**
 * Check if we missed any briefings and need to catch up.
 * Runs once at startup before the cron schedule takes over.
 */
async function checkCatchUp(): Promise<void> {
  const lastRun = getLastSuccessfulRun();

  if (!lastRun) {
    // No previous run — first time, pipeline will handle initial fetch
    logger.info("No previous successful run found. Will generate first briefing.");
    return;
  }

  const lastRunDate = new Date(lastRun.finished_at).toDateString();
  const todayDate = new Date().toDateString();

  if (lastRunDate === todayDate) {
    logger.info("Today's briefing already generated.");
    return;
  }

  const daysSince = Math.ceil((Date.now() - lastRun.finished_at) / 86400000);

  if (daysSince > 1) {
    logger.info(
      `Missed ${daysSince} day(s) of briefings. Running catch-up pipeline...`
    );
  } else {
    logger.info("Missed yesterday's briefing. Running catch-up...");
  }

  try {
    await runPipeline("catchup");
  } catch (error: any) {
    logger.error(`Catch-up pipeline failed: ${error.message}`);
  }
}

/**
 * Start the scheduler daemon.
 * 1. Check for missed briefings (catch-up)
 * 2. Register the cron job for daily execution
 * 3. Keep the process alive
 */
export async function startScheduler(): Promise<void> {
  const config = loadAppConfig();
  const { dailyBriefing, timezone } = config.schedule;

  logger.info(`LedgerClaw scheduler starting...`);
  logger.info(`Schedule: ${dailyBriefing} (${timezone})`);

  // Check for catch-up on startup
  await checkCatchUp();

  // Register cron job
  const isValid = cron.validate(dailyBriefing);
  if (!isValid) {
    throw new Error(`Invalid cron expression: ${dailyBriefing}`);
  }

  cron.schedule(
    dailyBriefing,
    async () => {
      logger.info("─── Scheduled pipeline triggered ───");
      try {
        await runPipeline("scheduled");
      } catch (error: any) {
        logger.error(`Scheduled pipeline failed: ${error.message}`);
      }
    },
    { timezone }
  );

  logger.info("Scheduler running. Waiting for next trigger...");
  logger.info("Press Ctrl+C to stop.\n");

  // Keep process alive
  process.on("SIGINT", () => {
    logger.info("Scheduler shutting down...");
    process.exit(0);
  });

  process.on("SIGTERM", () => {
    logger.info("Scheduler received SIGTERM, shutting down...");
    process.exit(0);
  });
}
