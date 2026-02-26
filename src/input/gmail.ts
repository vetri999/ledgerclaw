import { google, gmail_v1 } from "googleapis";
import fs from "fs";
import http from "http";
import open from "open";
import { PATHS } from "../utils/paths.js";
import { loadAppConfig } from "../utils/config.js";
import * as db from "../store/database.js";
import logger from "../utils/logger.js";

const SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"];

// ─── OAuth2 ─────────────────────────────────────────────────────────

function createOAuth2Client() {
  const raw = JSON.parse(fs.readFileSync(PATHS.gmailCredentials, "utf-8"));
  const creds = raw.installed || raw.web;
  return new google.auth.OAuth2(creds.client_id, creds.client_secret, "http://localhost:3377");
}

function loadTokens(client: any): boolean {
  if (!fs.existsSync(PATHS.gmailToken)) return false;
  const tokens = JSON.parse(fs.readFileSync(PATHS.gmailToken, "utf-8"));
  client.setCredentials(tokens);
  client.on("tokens", (newTokens: any) => {
    const merged = { ...tokens, ...newTokens };
    fs.writeFileSync(PATHS.gmailToken, JSON.stringify(merged, null, 2));
    logger.info("Gmail OAuth token refreshed");
  });
  return true;
}

export async function setupGmailAuth(): Promise<void> {
  if (!fs.existsSync(PATHS.gmailCredentials)) {
    throw new Error(
      `Gmail credentials not found at ${PATHS.gmailCredentials}.\n` +
        "Download OAuth2 credentials from Google Cloud Console and place the file there."
    );
  }

  const client = createOAuth2Client();
  const authUrl = client.generateAuthUrl({
    access_type: "offline",
    scope: SCOPES,
    prompt: "consent",
  });

  logger.info("Opening browser for Gmail authorization...");

  const code = await new Promise<string>((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url || "", "http://localhost:3377");
      const authCode = url.searchParams.get("code");
      if (authCode) {
        res.writeHead(200, { "Content-Type": "text/html" });
        res.end("<h2>✅ LedgerClaw authorized! You can close this tab.</h2>");
        server.close();
        resolve(authCode);
      } else {
        res.writeHead(400, { "Content-Type": "text/html" });
        res.end("<h2>❌ Authorization failed.</h2>");
        server.close();
        reject(new Error("No auth code received"));
      }
    });
    server.listen(3377, () => {
      open(authUrl).catch(() => {
        logger.info(`Open this URL in your browser:\n${authUrl}`);
      });
    });
    setTimeout(() => {
      server.close();
      reject(new Error("Gmail auth timed out (5 min)"));
    }, 5 * 60 * 1000);
  });

  const { tokens } = await client.getToken(code);
  client.setCredentials(tokens);
  fs.writeFileSync(PATHS.gmailToken, JSON.stringify(tokens, null, 2));

  const gmail = google.gmail({ version: "v1", auth: client });
  const profile = await gmail.users.getProfile({ userId: "me" });
  logger.info(`Gmail connected as ${profile.data.emailAddress}`);
}

// ─── Body Extraction ────────────────────────────────────────────────

function extractPlainText(payload: gmail_v1.Schema$MessagePart): string | null {
  if (!payload) return null;

  if (payload.mimeType === "text/plain" && payload.body?.data) {
    return Buffer.from(payload.body.data, "base64url").toString("utf-8");
  }

  if (payload.parts) {
    for (const part of payload.parts) {
      const text = extractPlainText(part);
      if (text) return text;
    }
  }

  if (payload.mimeType === "text/html" && payload.body?.data) {
    const html = Buffer.from(payload.body.data, "base64url").toString("utf-8");
    return html
      .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
      .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  return null;
}

function getHeader(headers: gmail_v1.Schema$MessagePartHeader[] | undefined, name: string): string {
  if (!headers) return "";
  const h = headers.find((h) => h.name?.toLowerCase() === name.toLowerCase());
  return h?.value || "";
}

function extractSenderAddress(from: string): string {
  const match = from.match(/<(.+?)>/);
  return (match ? match[1] : from).toLowerCase().trim();
}

function extractSenderName(from: string): string | null {
  const match = from.match(/^(.+?)\s*</);
  return match ? match[1].replace(/"/g, "").trim() : null;
}

// ─── Batch Fetching ─────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchWithRetry<T>(fn: () => Promise<T>, maxRetries: number): Promise<T> {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error: any) {
      const status = error?.response?.status || error?.code;
      const retryable = status === 429 || status === 500 || status === 502 || status === 503;
      if (!retryable || attempt === maxRetries) throw error;
      const delay = Math.pow(2, attempt) * 1000;
      logger.warn(`Gmail API ${status}, retry in ${delay}ms (${attempt + 1}/${maxRetries})`);
      await sleep(delay);
    }
  }
  throw new Error("Unreachable");
}

async function fetchMessagesBatched(
  gmail: gmail_v1.Gmail,
  ids: string[],
  batchSize: number,
  batchDelayMs: number,
  maxRetries: number
): Promise<number> {
  let count = 0;
  const totalBatches = Math.ceil(ids.length / batchSize);

  for (let i = 0; i < ids.length; i += batchSize) {
    const batch = ids.slice(i, i + batchSize);
    const batchNum = Math.floor(i / batchSize) + 1;
    logger.info(`Fetching emails: batch ${batchNum}/${totalBatches} (${batch.length} emails)`);

    for (const msgId of batch) {
      if (db.emailExists(msgId)) continue;

      try {
        const msg = await fetchWithRetry(
          () => gmail.users.messages.get({ userId: "me", id: msgId, format: "full" }),
          maxRetries
        );

        const data = msg.data;
        const headers = data.payload?.headers;
        const fromRaw = getHeader(headers, "From");

        db.insertEmail({
          id: data.id!,
          thread_id: data.threadId || null,
          sender: extractSenderAddress(fromRaw),
          sender_name: extractSenderName(fromRaw),
          subject: getHeader(headers, "Subject") || "(no subject)",
          body_text: data.payload ? extractPlainText(data.payload) : null,
          received_at: parseInt(data.internalDate || "0"),
          fetched_at: Date.now(),
          labels: (data.labelIds || []).join(","),
        });

        count++;
      } catch (error: any) {
        logger.warn(`Skipping email ${msgId}: ${error.message}`);
      }
    }

    // Pause between batches for rate limit safety
    if (i + batchSize < ids.length) {
      await sleep(batchDelayMs);
    }
  }

  return count;
}

// ─── Fetch Strategies ───────────────────────────────────────────────

async function fetchViaHistory(gmail: gmail_v1.Gmail, startHistoryId: string): Promise<string[]> {
  const allIds: string[] = [];
  let pageToken: string | undefined;

  do {
    const res = await fetchWithRetry(
      () =>
        gmail.users.history.list({
          userId: "me",
          startHistoryId,
          historyTypes: ["messageAdded"],
          pageToken,
        }),
      3
    );

    if (res.data.history) {
      for (const record of res.data.history) {
        if (record.messagesAdded) {
          for (const added of record.messagesAdded) {
            if (added.message?.id) allIds.push(added.message.id);
          }
        }
      }
    }
    pageToken = res.data.nextPageToken || undefined;
  } while (pageToken);

  return [...new Set(allIds)];
}

async function fetchViaList(
  gmail: gmail_v1.Gmail,
  daysBack: number,
  maxPerPage: number
): Promise<string[]> {
  const after = new Date(Date.now() - daysBack * 86400000);
  const afterStr = `${after.getFullYear()}/${String(after.getMonth() + 1).padStart(2, "0")}/${String(after.getDate()).padStart(2, "0")}`;

  const allIds: string[] = [];
  let pageToken: string | undefined;

  do {
    const res = await fetchWithRetry(
      () =>
        gmail.users.messages.list({
          userId: "me",
          q: `after:${afterStr}`,
          maxResults: maxPerPage,
          pageToken,
        }),
      3
    );

    if (res.data.messages) {
      for (const msg of res.data.messages) {
        if (msg.id) allIds.push(msg.id);
      }
    }
    pageToken = res.data.nextPageToken || undefined;
  } while (pageToken);

  return allIds;
}

// ─── Public Interface ───────────────────────────────────────────────

export interface FetchResult {
  count: number;
  emailIds: string[];
}

export async function fetchEmails(): Promise<FetchResult> {
  const config = loadAppConfig();
  const { batchSize, batchDelayMs, maxRetries } = config.gmail;
  const client = createOAuth2Client();

  if (!loadTokens(client)) {
    throw new Error("Gmail not authenticated. Run onboarding first.");
  }

  const gmail = google.gmail({ version: "v1", auth: client });
  const syncState = db.getSyncState("gmail");

  let messageIds: string[];

  if (syncState?.last_history_id) {
    logger.info("Fetching new emails via History API...");
    try {
      messageIds = await fetchViaHistory(gmail, syncState.last_history_id);
    } catch (error: any) {
      if (error?.response?.status === 404) {
        logger.warn("History ID expired, falling back to full fetch");
        messageIds = await fetchViaList(gmail, config.gmail.initialFetchDays, config.gmail.maxResultsPerPage);
      } else {
        throw error;
      }
    }
  } else {
    logger.info(`First fetch: getting last ${config.gmail.initialFetchDays} days of emails...`);
    messageIds = await fetchViaList(gmail, config.gmail.initialFetchDays, config.gmail.maxResultsPerPage);
  }

  logger.info(`Found ${messageIds.length} message IDs to process`);

  const fetchedCount = await fetchMessagesBatched(gmail, messageIds, batchSize, batchDelayMs, maxRetries);

  // Update sync state
  const profile = await gmail.users.getProfile({ userId: "me" });
  const emails = messageIds.length > 0 ? db.getEmailsByIds(messageIds.slice(0, 1)) : [];
  const latestTs = emails.length > 0 ? emails[0].received_at : syncState?.last_message_ts || Date.now();

  db.upsertSyncState({
    source: "gmail",
    last_history_id: profile.data.historyId || null,
    last_fetched_at: Date.now(),
    last_message_ts: latestTs,
  });

  logger.info(`Fetched ${fetchedCount} new emails`);
  return { count: fetchedCount, emailIds: messageIds };
}
