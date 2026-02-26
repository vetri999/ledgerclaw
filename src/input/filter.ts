import * as db from "../store/database.js";
import {
  loadDefaultRules,
  loadUserRules,
  saveUserRules,
  type DefaultRules,
  type UserRules,
} from "../utils/config.js";
import { PATHS } from "../utils/paths.js";
import { buildPrompt } from "../process/prompt-creator.js";
import { callLLM } from "../process/llm-interface.js";
import logger from "../utils/logger.js";

// ─── Pattern Matching ───────────────────────────────────────────────

function matchesSenderPattern(address: string, pattern: string): boolean {
  const addr = address.toLowerCase();
  const pat = pattern.toLowerCase();

  if (pat.startsWith("*")) {
    // Wildcard: "*@hdfcbank.net" matches "alerts@hdfcbank.net"
    return addr.endsWith(pat.substring(1));
  }
  return addr === pat;
}

function matchesSenderList(address: string, patterns: string[]): boolean {
  return patterns.some((p) => matchesSenderPattern(address, p));
}

function containsKeyword(text: string, keywords: string[]): boolean {
  const lower = text.toLowerCase();
  return keywords.some((kw) => lower.includes(kw.toLowerCase()));
}

function countKeywordMatches(text: string, keywords: string[]): number {
  const lower = text.toLowerCase();
  return keywords.filter((kw) => lower.includes(kw.toLowerCase())).length;
}

// ─── Category Detection ─────────────────────────────────────────────

function detectCategory(
  sender: string,
  subject: string,
  categories: DefaultRules["categories"]
): string | null {
  const senderLower = sender.toLowerCase();
  const subjectLower = subject.toLowerCase();

  for (const [category, hints] of Object.entries(categories)) {
    const senderMatch = hints.senderHints.some((h) => senderLower.includes(h.toLowerCase()));
    const keywordMatch = hints.keywordHints.some((h) => subjectLower.includes(h.toLowerCase()));
    if (senderMatch || keywordMatch) return category;
  }

  return "other_financial";
}

// ─── Daily Classification (Rules-based, no LLM) ────────────────────

export interface ClassifyResult {
  total: number;
  financialCount: number;
  financialEmailIds: string[];
  tokensUsed: number;
}

export function classifyEmails(emailIds: string[]): ClassifyResult {
  const defaultRules = loadDefaultRules();
  const userRules = loadUserRules();

  // Merge all rule lists
  const financialSenders = [
    ...defaultRules.senders.financial,
    ...(userRules?.senders.financial || []),
  ];
  const ignoreSenders = [
    ...defaultRules.senders.ignore,
    ...(userRules?.senders.ignore || []),
  ];
  const subjectKeywords = [
    ...defaultRules.keywords.subject,
    ...(userRules?.keywords.subject || []),
  ];
  const bodyKeywords = [
    ...defaultRules.keywords.body,
    ...(userRules?.keywords.body || []),
  ];

  const result: ClassifyResult = {
    total: 0,
    financialCount: 0,
    financialEmailIds: [],
    tokensUsed: 0,
  };

  for (const emailId of emailIds) {
    // Skip already classified
    if (db.getClassification(emailId)) {
      result.total++;
      const existing = db.getClassification(emailId)!;
      if (existing.is_financial) {
        result.financialCount++;
        result.financialEmailIds.push(emailId);
      }
      continue;
    }

    const emails = db.getEmailsByIds([emailId]);
    if (emails.length === 0) continue;
    const email = emails[0];

    // CHECK 1: Ignore list (marketing emails from financial domains)
    if (matchesSenderList(email.sender, ignoreSenders)) {
      db.insertClassification({
        email_id: emailId,
        is_financial: 0,
        category: null,
        confidence: 1.0,
        classified_by: "rules_ignore",
        classified_at: Date.now(),
      });
      result.total++;
      continue;
    }

    // CHECK 2: Financial sender match (default patterns OR user's exact senders)
    if (matchesSenderList(email.sender, financialSenders)) {
      const category = detectCategory(email.sender, email.subject, defaultRules.categories);
      db.insertClassification({
        email_id: emailId,
        is_financial: 1,
        category,
        confidence: 1.0,
        classified_by: "rules_sender",
        classified_at: Date.now(),
      });
      result.total++;
      result.financialCount++;
      result.financialEmailIds.push(emailId);
      continue;
    }

    // CHECK 3: Subject keyword match
    if (containsKeyword(email.subject, subjectKeywords)) {
      const category = detectCategory(email.sender, email.subject, defaultRules.categories);
      db.insertClassification({
        email_id: emailId,
        is_financial: 1,
        category,
        confidence: 0.85,
        classified_by: "rules_keyword",
        classified_at: Date.now(),
      });
      result.total++;
      result.financialCount++;
      result.financialEmailIds.push(emailId);
      continue;
    }

    // CHECK 4: Body keyword match (require at least 2 matches for confidence)
    if (email.body_text) {
      const bodyText = email.body_text.substring(0, 1500);
      const matchCount = countKeywordMatches(bodyText, bodyKeywords);
      if (matchCount >= 2) {
        const category = detectCategory(email.sender, email.subject, defaultRules.categories);
        db.insertClassification({
          email_id: emailId,
          is_financial: 1,
          category,
          confidence: 0.7,
          classified_by: "rules_keyword",
          classified_at: Date.now(),
        });
        result.total++;
        result.financialCount++;
        result.financialEmailIds.push(emailId);
        continue;
      }
    }

    // NO MATCH: classify as not financial
    db.insertClassification({
      email_id: emailId,
      is_financial: 0,
      category: null,
      confidence: 0.8,
      classified_by: "rules_no_match",
      classified_at: Date.now(),
    });
    result.total++;
  }

  logger.info(
    `Classified ${result.total} emails: ${result.financialCount} financial, ${result.total - result.financialCount} non-financial`
  );

  return result;
}

// ─── Organic Filter Building (Onboarding + Periodic Refresh) ────────

export async function buildOrganicFilter(): Promise<number> {
  logger.info("Building organic filter from your email data...");

  let totalTokens = 0;

  // STEP 1: Get all unique senders from the database
  const allSenders = db.getAllUniqueSenders();
  logger.info(`Found ${allSenders.length} unique senders in your inbox`);

  if (allSenders.length === 0) {
    logger.warn("No emails in database. Skipping organic filter build.");
    return 0;
  }

  // STEP 2: Pre-filter with default rules — identify already-known senders
  const defaultRules = loadDefaultRules();
  const knownFinancial: string[] = [];
  const unknownSenders: string[] = [];

  for (const sender of allSenders) {
    if (matchesSenderList(sender, defaultRules.senders.ignore)) {
      continue; // Skip known marketing senders
    }
    if (matchesSenderList(sender, defaultRules.senders.financial)) {
      knownFinancial.push(sender);
    } else {
      unknownSenders.push(sender);
    }
  }

  logger.info(
    `${knownFinancial.length} senders matched default rules, ${unknownSenders.length} need LLM classification`
  );

  // STEP 3: Send unknown senders to LLM in batches (max 100 per call)
  const llmFinancial: string[] = [];
  const llmIgnore: string[] = [];
  const SENDER_BATCH_SIZE = 100;

  for (let i = 0; i < unknownSenders.length; i += SENDER_BATCH_SIZE) {
    const batch = unknownSenders.slice(i, i + SENDER_BATCH_SIZE);
    const batchNum = Math.floor(i / SENDER_BATCH_SIZE) + 1;
    const totalBatches = Math.ceil(unknownSenders.length / SENDER_BATCH_SIZE);

    logger.info(`Classifying senders: batch ${batchNum}/${totalBatches} (${batch.length} senders)`);

    const senderListText = batch.map((s, idx) => `${idx + 1}. ${s}`).join("\n");

    try {
      const prompt = buildPrompt(PATHS.promptClassifySenders, {
        sender_list: senderListText,
      });

      const response = await callLLM({
        prompt,
        responseFormat: "json",
        temperature: 0.1,
        maxTokens: 2000,
      });

      totalTokens += response.tokensUsed;

      // Parse LLM response
      const cleaned = response.text.replace(/```json\n?|```/g, "").trim();
      const parsed = JSON.parse(cleaned);

      if (parsed.financial && Array.isArray(parsed.financial)) {
        llmFinancial.push(...parsed.financial.map((s: string) => s.toLowerCase()));
      }
      // Ignore list from LLM is optional
      if (parsed.non_financial && Array.isArray(parsed.non_financial)) {
        // We don't actively block these, just don't add them to financial
      }
    } catch (error: any) {
      logger.warn(`LLM sender classification failed for batch ${batchNum}: ${error.message}`);
      // Continue with other batches
    }
  }

  // Combine all financial senders
  const allFinancialSenders = [...new Set([...knownFinancial, ...llmFinancial])];
  logger.info(`Total financial senders identified: ${allFinancialSenders.length}`);

  // STEP 4: Extract financial keywords from actual email subjects
  const financialSubjects = db.getSubjectsBySenders(allFinancialSenders);
  logger.info(`Extracting keywords from ${financialSubjects.length} financial email subjects`);

  let extractedSubjectKeywords: string[] = [];
  let extractedBodyKeywords: string[] = [];

  if (financialSubjects.length > 0) {
    // Send subjects in batches of 200
    const SUBJECT_BATCH_SIZE = 200;
    const subjectSample = financialSubjects.slice(0, SUBJECT_BATCH_SIZE);
    const subjectText = subjectSample.map((s, i) => `${i + 1}. ${s}`).join("\n");

    try {
      const prompt = buildPrompt(PATHS.promptExtractKeywords, {
        subject_lines: subjectText,
      });

      const response = await callLLM({
        prompt,
        responseFormat: "json",
        temperature: 0.2,
        maxTokens: 1500,
      });

      totalTokens += response.tokensUsed;

      const cleaned = response.text.replace(/```json\n?|```/g, "").trim();
      const parsed = JSON.parse(cleaned);

      if (parsed.subject_keywords && Array.isArray(parsed.subject_keywords)) {
        extractedSubjectKeywords = parsed.subject_keywords;
      }
      if (parsed.body_keywords && Array.isArray(parsed.body_keywords)) {
        extractedBodyKeywords = parsed.body_keywords;
      }
    } catch (error: any) {
      logger.warn(`LLM keyword extraction failed: ${error.message}`);
    }
  }

  // STEP 5: Save user rules
  const userRules: UserRules = {
    version: 1,
    generated_at: new Date().toISOString(),
    last_refreshed_at: new Date().toISOString(),
    senders: {
      financial: allFinancialSenders,
      ignore: llmIgnore,
      pending_review: [],
    },
    keywords: {
      subject: extractedSubjectKeywords,
      body: extractedBodyKeywords,
    },
  };

  saveUserRules(userRules);

  logger.info(
    `Organic filter built: ${allFinancialSenders.length} financial senders, ` +
      `${extractedSubjectKeywords.length} subject keywords, ` +
      `${extractedBodyKeywords.length} body keywords`
  );

  return totalTokens;
}

// ─── Periodic Refresh ───────────────────────────────────────────────

export async function refreshFilter(): Promise<number> {
  const userRules = loadUserRules();
  if (!userRules) {
    logger.info("No user rules exist. Running full organic filter build.");
    return buildOrganicFilter();
  }

  // Check if refresh is needed
  const lastRefresh = new Date(userRules.last_refreshed_at).getTime();
  const config = (await import("../utils/config.js")).loadAppConfig();
  const refreshInterval = config.filter.refreshIntervalDays * 86400000;

  if (Date.now() - lastRefresh < refreshInterval) {
    logger.info("Filter refresh not yet due. Skipping.");
    return 0;
  }

  logger.info("Running periodic filter refresh...");

  let totalTokens = 0;

  // Find senders not in the current lists
  const allSenders = db.getAllUniqueSenders();
  const knownSenders = new Set([
    ...userRules.senders.financial,
    ...userRules.senders.ignore,
  ]);

  const newSenders = allSenders.filter((s) => !knownSenders.has(s));

  // Also check against default rules
  const defaultRules = loadDefaultRules();
  const trulyNew = newSenders.filter(
    (s) =>
      !matchesSenderList(s, defaultRules.senders.financial) &&
      !matchesSenderList(s, defaultRules.senders.ignore)
  );

  if (trulyNew.length === 0) {
    logger.info("No new senders to classify. Filter up to date.");
    userRules.last_refreshed_at = new Date().toISOString();
    saveUserRules(userRules);
    return 0;
  }

  logger.info(`Found ${trulyNew.length} new senders to classify`);

  // Classify new senders via LLM
  const senderListText = trulyNew.map((s, i) => `${i + 1}. ${s}`).join("\n");

  try {
    const prompt = buildPrompt(PATHS.promptClassifySenders, {
      sender_list: senderListText,
    });

    const response = await callLLM({
      prompt,
      responseFormat: "json",
      temperature: 0.1,
      maxTokens: 2000,
    });

    totalTokens += response.tokensUsed;

    const cleaned = response.text.replace(/```json\n?|```/g, "").trim();
    const parsed = JSON.parse(cleaned);

    if (parsed.financial && Array.isArray(parsed.financial)) {
      const newFinancial = parsed.financial.map((s: string) => s.toLowerCase());
      userRules.senders.financial.push(...newFinancial);
      userRules.senders.pending_review.push(...newFinancial);
      logger.info(`Discovered ${newFinancial.length} new financial senders`);
    }
  } catch (error: any) {
    logger.warn(`Filter refresh LLM call failed: ${error.message}`);
  }

  // Deduplicate
  userRules.senders.financial = [...new Set(userRules.senders.financial)];
  userRules.last_refreshed_at = new Date().toISOString();
  saveUserRules(userRules);

  return totalTokens;
}
