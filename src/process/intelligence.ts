import * as db from "../store/database.js";
import { PATHS } from "../utils/paths.js";
import { buildPrompt, formatEmailsForPrompt } from "../process/prompt-creator.js";
import { callLLM } from "../process/llm-interface.js";
import logger from "../utils/logger.js";

export interface BriefingResult {
  briefingId: number;
  content: string;
  tokensUsed: number;
}

// ─── Action Extraction ──────────────────────────────────────────────

interface ActionItem {
  description: string;
  dueDate: string | null;
  priority: "urgent" | "soon" | "fyi";
}

function extractActions(briefingText: string): ActionItem[] {
  const actions: ActionItem[] = [];

  // Find the "Actions Required" section
  const parts = briefingText.split(/actions required/i);
  if (parts.length < 2) return actions;

  const actionsSection = parts[1];
  const lines = actionsSection.split("\n").filter((l) => l.trim());

  let currentPriority: ActionItem["priority"] = "fyi";

  for (const line of lines) {
    const trimmed = line.trim();

    // Detect priority from emoji markers
    if (trimmed.includes("🔴")) currentPriority = "urgent";
    else if (trimmed.includes("🟡")) currentPriority = "soon";
    else if (trimmed.includes("🟢")) currentPriority = "fyi";

    // Lines starting with - or • that contain action text
    if (/^[-•*]/.test(trimmed) || trimmed.includes("🔴") || trimmed.includes("🟡") || trimmed.includes("🟢")) {
      const description = trimmed
        .replace(/^[-•*]\s*/, "")
        .replace(/🔴|🟡|🟢/g, "")
        .trim();

      if (description && description.length > 5 && !description.toLowerCase().includes("no actions")) {
        // Try to extract due date
        const dateMatch = description.match(
          /(?:due|by|before|deadline)[:\s]*(\d{1,2}[\s/-]\w+[\s/-]?\d{0,4}|\w+ \d{1,2}(?:,?\s*\d{4})?)/i
        );
        let dueDate: string | null = null;
        if (dateMatch) {
          try {
            const parsed = new Date(dateMatch[1]);
            if (!isNaN(parsed.getTime())) {
              dueDate = parsed.toISOString().split("T")[0];
            }
          } catch {
            // Date parsing failed, leave as null
          }
        }

        actions.push({ description, dueDate, priority: currentPriority });
      }
    }
  }

  return actions;
}

// ─── Date Formatting ────────────────────────────────────────────────

function formatDate(ts: number): string {
  return new Date(ts).toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function getDayName(ts: number): string {
  return new Date(ts).toLocaleDateString("en-IN", { weekday: "long" });
}

// ─── Briefing Generation ────────────────────────────────────────────

export async function generateBriefing(
  financialEmailIds: string[],
  periodStart: number,
  periodEnd: number
): Promise<BriefingResult> {
  const now = Date.now();

  // Load financial emails with their classifications
  const emails = db.getEmailsByIds(financialEmailIds);
  const enrichedEmails = emails.map((e) => {
    const classification = db.getClassification(e.id);
    return {
      ...e,
      category: classification?.category || "other_financial",
    };
  });

  // Determine period description
  const startDate = formatDate(periodStart);
  const endDate = formatDate(periodEnd);
  const isSameDay = startDate === endDate;
  const periodDescription = isSameDay
    ? `${startDate} (${getDayName(periodEnd)})`
    : `${startDate} to ${endDate} (${Math.ceil((periodEnd - periodStart) / 86400000)} days)`;

  // Format emails for the prompt
  const emailsText = formatEmailsForPrompt(enrichedEmails);

  // Estimate tokens: ~1 token per 4 chars
  const estimatedTokens = emailsText.length / 4;
  let totalTokens = 0;
  let briefingText: string;

  if (estimatedTokens <= 6000) {
    // ─── Single Call Path ─────────────────────────────────────
    logger.info(`Generating briefing (single call, ~${Math.round(estimatedTokens)} estimated input tokens)`);

    const prompt = buildPrompt(PATHS.promptBriefing, {
      date: endDate,
      day: getDayName(periodEnd),
      period_description: periodDescription,
      financial_emails: emailsText,
    });

    const response = await callLLM({
      prompt,
      temperature: 0.4,
      maxTokens: 1500,
    });

    briefingText = response.text;
    totalTokens = response.tokensUsed;
  } else {
    // ─── Batched Path ─────────────────────────────────────────
    logger.info(`Many emails (${enrichedEmails.length}), using batched briefing generation`);

    const BATCH_SIZE = 20;
    const chunks: typeof enrichedEmails[] = [];
    for (let i = 0; i < enrichedEmails.length; i += BATCH_SIZE) {
      chunks.push(enrichedEmails.slice(i, i + BATCH_SIZE));
    }

    const partialSummaries: string[] = [];

    for (let i = 0; i < chunks.length; i++) {
      logger.info(`Generating partial summary ${i + 1}/${chunks.length}...`);

      const chunkText = formatEmailsForPrompt(chunks[i]);
      const prompt = buildPrompt(PATHS.promptBriefing, {
        date: endDate,
        day: getDayName(periodEnd),
        period_description: periodDescription,
        financial_emails: chunkText,
      });

      const response = await callLLM({
        prompt,
        temperature: 0.4,
        maxTokens: 800,
      });

      partialSummaries.push(response.text);
      totalTokens += response.tokensUsed;
    }

    // Consolidation pass
    logger.info("Consolidating partial summaries into final briefing...");

    const consolidationPrompt = buildPrompt(PATHS.promptBriefing, {
      date: endDate,
      day: getDayName(periodEnd),
      period_description: periodDescription,
      financial_emails:
        "Below are partial summaries from today's financial emails. Consolidate into one cohesive briefing.\n\n" +
        partialSummaries.join("\n\n---\n\n"),
    });

    const finalResponse = await callLLM({
      prompt: consolidationPrompt,
      temperature: 0.3,
      maxTokens: 1500,
    });

    briefingText = finalResponse.text;
    totalTokens += finalResponse.tokensUsed;
  }

  // Extract action items
  const actions = extractActions(briefingText);

  // Store briefing
  const briefingId = db.insertBriefing({
    generated_at: now,
    period_start: periodStart,
    period_end: periodEnd,
    email_count: financialEmailIds.length,
    content: briefingText,
    model_used: null, // Will be populated from response
    tokens_used: totalTokens,
  });

  // Store actions
  for (const action of actions) {
    db.insertAction({
      briefing_id: briefingId,
      email_id: null,
      description: action.description,
      due_date: action.dueDate,
      priority: action.priority,
      created_at: now,
    });
  }

  logger.info(
    `Briefing generated: ${briefingText.length} chars, ${actions.length} actions, ${totalTokens} tokens`
  );

  return { briefingId, content: briefingText, tokensUsed: totalTokens };
}
