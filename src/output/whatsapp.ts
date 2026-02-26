import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import { PATHS } from "../utils/paths.js";
import { loadAppConfig, saveAppConfig } from "../utils/config.js";
import logger from "../utils/logger.js";
import fs from "fs";

// ─── Formatting ─────────────────────────────────────────────────────

function formatForWhatsApp(markdown: string): string {
  let text = markdown;

  // ## Heading → *Heading*
  text = text.replace(/^## (.+)$/gm, "*$1*");
  text = text.replace(/^### (.+)$/gm, "_$1_");

  // **bold** → *bold*
  text = text.replace(/\*\*(.+?)\*\*/g, "*$1*");

  // - bullet → • bullet
  text = text.replace(/^- /gm, "• ");

  // Clean excessive whitespace
  text = text.replace(/\n{3,}/g, "\n\n");

  return text.trim();
}

function splitMessage(text: string, maxLength: number = 1800): string[] {
  if (text.length <= maxLength) return [text];

  const messages: string[] = [];
  // Split at bold headings (*...*\n)
  const sections = text.split(/(?=\*[^*\n]+\*\n)/);

  let current = "";
  for (const section of sections) {
    if (current.length + section.length > maxLength) {
      if (current) messages.push(current.trim());
      current = section;
    } else {
      current += section;
    }
  }
  if (current) messages.push(current.trim());

  return messages;
}

// ─── Sleep Utility ──────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ─── WhatsApp Setup (Onboarding) ────────────────────────────────────

export async function setupWhatsApp(): Promise<void> {
  fs.mkdirSync(PATHS.whatsappAuth, { recursive: true });

  logger.info("Starting WhatsApp setup. A QR code will appear below.");
  logger.info("Open WhatsApp on your phone → Settings → Linked Devices → Link a Device");
  logger.info("Scan the QR code shown in this terminal.\n");

  const { state, saveCreds } = await useMultiFileAuthState(PATHS.whatsappAuth);
  const { version } = await fetchLatestBaileysVersion();

  return new Promise((resolve, reject) => {
    const sock = makeWASocket({
      version,
      auth: state,
      printQRInTerminal: true,
      browser: ["LedgerClaw", "Desktop", "1.0.0"],
    });

    const timeout = setTimeout(() => {
      sock.end(undefined);
      reject(new Error("WhatsApp setup timed out (3 min). Please try again."));
    }, 3 * 60 * 1000);

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect } = update;

      if (connection === "open") {
        clearTimeout(timeout);
        const jid = sock.user?.id;
        logger.info(`WhatsApp connected successfully!`);

        if (jid) {
          // Save the user's JID for sending messages to self
          const config = loadAppConfig();
          config.delivery.recipientJid = jid;
          saveAppConfig(config);
          logger.info(`Recipient JID saved: ${jid}`);
        }

        // Disconnect after setup
        setTimeout(() => {
          sock.end(undefined);
          resolve();
        }, 2000);
      }

      if (connection === "close") {
        const reason = (lastDisconnect?.error as Boom)?.output?.statusCode;
        if (reason === DisconnectReason.loggedOut) {
          clearTimeout(timeout);
          reject(new Error("WhatsApp logged out. Please try setup again."));
        }
        // Other disconnects during setup are expected after we call sock.end()
      }
    });
  });
}

// ─── Send Briefing ──────────────────────────────────────────────────

export async function sendBriefing(briefingContent: string): Promise<void> {
  const config = loadAppConfig();
  const recipientJid = config.delivery.recipientJid;

  if (!recipientJid) {
    throw new Error("No WhatsApp recipient configured. Run onboarding first.");
  }

  if (!fs.existsSync(PATHS.whatsappAuth)) {
    throw new Error("WhatsApp auth not found. Run onboarding first.");
  }

  const { state, saveCreds } = await useMultiFileAuthState(PATHS.whatsappAuth);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    browser: ["LedgerClaw", "Desktop", "1.0.0"],
  });

  sock.ev.on("creds.update", saveCreds);

  // Wait for connection
  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => {
      sock.end(undefined);
      reject(new Error("WhatsApp connection timed out (30s)"));
    }, 30000);

    sock.ev.on("connection.update", (update) => {
      if (update.connection === "open") {
        clearTimeout(timeout);
        resolve();
      }
      if (update.connection === "close") {
        const reason = (update.lastDisconnect?.error as Boom)?.output?.statusCode;
        if (reason === DisconnectReason.loggedOut) {
          clearTimeout(timeout);
          reject(
            new Error(
              "WhatsApp session expired. Re-scan QR code by running: ledgerclaw onboard"
            )
          );
        }
      }
    });
  });

  try {
    // Format and split
    const formatted = formatForWhatsApp(briefingContent);
    const messages = splitMessage(formatted);

    const today = new Date().toLocaleDateString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });

    for (let i = 0; i < messages.length; i++) {
      let header: string;
      if (messages.length > 1) {
        header = `📋 *Daily Finance Briefing* — ${today} (${i + 1}/${messages.length})\n\n`;
      } else {
        header = `📋 *Daily Finance Briefing* — ${today}\n\n`;
      }

      await sock.sendMessage(recipientJid, { text: header + messages[i] });

      if (i < messages.length - 1) {
        await sleep(1000);
      }
    }

    logger.info(`Briefing delivered via WhatsApp (${messages.length} message(s))`);
  } finally {
    // Always disconnect gracefully
    await sleep(2000);
    sock.end(undefined);
  }
}
