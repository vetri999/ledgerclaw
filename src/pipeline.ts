import * as db from "../store/database.js";
import { fetchEmails } from "../input/gmail.js";
import { classifyEmails, refreshFilter } from "../input/filter.js";
import { generateBriefing } from "../process/intelligence.js";
import { sendBriefing } from "../output/whatsapp.js";
import { loadAppConfig } from "../utils/config.js";
import logger from "../utils/logger.js";

export async function runPipeline(trigger: "scheduled" | "manual" | "catchup"): Promise<void> {
  const runId = db.createPipelineRun(trigger);
  let totalTokens = 0;

  try {
    // ─── Determine period ───────────────────────────────────
    const lastRun = db.getLastSuccessfulRun();
    const now = Date.now();

    // Start of today (midnight in local terms, approximate with UTC)
    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);

    let periodStart: number;

    if (lastRun?.period_end) {
      // Resume from where we left off
      periodStart = lastRun.period_end;
    } else {
      // First ever run — cover initialFetchDays
      const config = loadAppConfig();
      periodStart = now - config.gmail.initialFetchDays * 86400000;
    }

    const periodEnd = now;

    // Check if we already ran today
    if (lastRun?.finished_at) {
      const lastRunDate = new Date(lastRun.finished_at).toDateString();
      const todayDate = new Date().toDateString();
      if (lastRunDate === todayDate && trigger === "scheduled") {
        logger.info("Today's briefing already generated. Skipping.");
        db.updatePipelineRun(runId, {
          status: "skipped",
          finished_at: now,
          period_start: periodStart,
          period_end: periodEnd,
        });
        return;
      }
    }

    const missedDays = Math.ceil((periodEnd - periodStart) / 86400000);
    if (missedDays > 1) {
      logger.info(`Catch-up mode: covering ${missedDays} days of emails`);
    }

    db.updatePipelineRun(runId, { period_start: periodStart, period_end: periodEnd });

    // ─── Step 1: Fetch emails ───────────────────────────────
    logger.info("─── Step 1: Fetching emails from Gmail ───");
    const fetchResult = await fetchEmails();
    db.updatePipelineRun(runId, { emails_fetched: fetchResult.count });

    // ─── Step 1.5: Periodic filter refresh ──────────────────
    const refreshTokens = await refreshFilter();
    totalTokens += refreshTokens;

    // ─── Step 2: Classify emails ────────────────────────────
    logger.info("─── Step 2: Classifying emails ───");
    const unclassifiedIds = db.getUnclassifiedEmailIds();
    const classifyResult = classifyEmails(unclassifiedIds);

    db.updatePipelineRun(runId, {
      emails_classified: classifyResult.total,
      emails_financial: classifyResult.financialCount,
    });

    // Get all financial emails in the period (not just newly classified)
    const financialIds = db.getFinancialEmailIdsSince(periodStart);

    if (financialIds.length === 0) {
      logger.info("No financial emails found in this period. Skipping briefing.");
      db.updatePipelineRun(runId, {
        status: "skipped",
        finished_at: Date.now(),
        tokens_used: totalTokens,
      });
      return;
    }

    logger.info(`Found ${financialIds.length} financial emails for briefing`);

    // ─── Step 3: Generate briefing ──────────────────────────
    logger.info("─── Step 3: Generating briefing ───");
    const briefingResult = await generateBriefing(financialIds, periodStart, periodEnd);
    totalTokens += briefingResult.tokensUsed;

    db.updatePipelineRun(runId, {
      tokens_used: totalTokens,
      briefing_id: briefingResult.briefingId,
    });

    // ─── Step 4: Deliver ────────────────────────────────────
    logger.info("─── Step 4: Delivering briefing ───");
    try {
      await sendBriefing(briefingResult.content);
      const config = loadAppConfig();
      db.markBriefingDelivered(briefingResult.briefingId, config.delivery.channel);
    } catch (deliveryError: any) {
      logger.error(`Delivery failed: ${deliveryError.message}`);
      db.markBriefingFailed(briefingResult.briefingId);
      // Mark as partial — briefing generated but not delivered
      db.updatePipelineRun(runId, {
        status: "partial",
        finished_at: Date.now(),
        tokens_used: totalTokens,
        error: `Delivery failed: ${deliveryError.message}`,
      });
      return;
    }

    // ─── Done ───────────────────────────────────────────────
    db.updatePipelineRun(runId, {
      status: "success",
      finished_at: Date.now(),
      tokens_used: totalTokens,
    });

    logger.info(`✅ Pipeline completed successfully. Tokens used: ${totalTokens}`);
  } catch (error: any) {
    logger.error(`Pipeline failed: ${error.message}`);
    db.updatePipelineRun(runId, {
      status: "failed",
      finished_at: Date.now(),
      tokens_used: totalTokens,
      error: error.message,
    });
    throw error;
  }
}
