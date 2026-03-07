/**
 * WhatsApp — Raw Baileys operations for LedgerClaw.
 *
 * Minimal wrapper. No formatting, no splitting, no config logic.
 * All paths and settings come from command line args (passed by Python bridge).
 *
 * Commands:
 *   setup  --auth-dir X --config-path X --group-name X --qr-timeout X
 *   send   --auth-dir X --group-jid X --timeout X  (message from stdin)
 *   listen --auth-dir X --group-jid X
 */

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  type WASocket,
} from "@whiskeysockets/baileys";
import fs from "fs";

// ─────────────────────────────────────────────────────────────────
// ARGS PARSER
// ─────────────────────────────────────────────────────────────────

function parseArgs(): Record<string, string> {
  const args: Record<string, string> = {};
  const raw = process.argv.slice(2);
  // First arg is the command (setup/send/listen)
  if (raw.length > 0 && !raw[0].startsWith("--")) {
    args._command = raw[0];
  }
  for (let i = 0; i < raw.length; i++) {
    if (raw[i].startsWith("--") && i + 1 < raw.length && !raw[i + 1].startsWith("--")) {
      args[raw[i].slice(2)] = raw[i + 1];
      i++;
    }
  }
  return args;
}

// ─────────────────────────────────────────────────────────────────
// 1. CONNECT — shared connection logic
// ─────────────────────────────────────────────────────────────────

// Suppress Baileys internal logs
const silentLogger = {
  level: "silent",
  child: () => silentLogger,
  trace: () => {}, debug: () => {}, info: () => {},
  warn: () => {}, error: () => {}, fatal: () => {},
} as any;

async function connect(
  authDir: string,
  showQR: boolean,
  timeoutMs: number
): Promise<WASocket> {
  fs.mkdirSync(authDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  const { version } = await fetchLatestBaileysVersion();

  // Baileys may intentionally close once with restartRequired (515) during login.
  const maxConnectAttempts = 3;
  let lastCode: number | undefined;

  for (let attempt = 1; attempt <= maxConnectAttempts; attempt++) {
    const sock = makeWASocket({
      version,
      auth: state,
      browser: ["LedgerClaw", "Desktop", "1.0.0"],
      logger: silentLogger,
    });

    sock.ev.on("creds.update", saveCreds);

    const outcome = await new Promise<
      { status: "open" } | { status: "close"; code?: number }
    >((resolve, reject) => {
      const timer = setTimeout(() => {
        sock.end(undefined);
        reject(new Error(showQR ? "QR scan timed out." : "Connection timed out."));
      }, timeoutMs);

      sock.ev.on("connection.update", async (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (showQR && qr) {
          const qrcode = await import("qrcode-terminal");
          qrcode.default.generate(qr, { small: true });
        }

        if (connection === "open") {
          clearTimeout(timer);
          resolve({ status: "open" });
          return;
        }

        if (connection === "close") {
          clearTimeout(timer);
          const code = (lastDisconnect?.error as any)?.output?.statusCode as
            | number
            | undefined;
          resolve({ status: "close", code });
        }
      });
    });

    if (outcome.status === "open") {
      return sock;
    }

    lastCode = outcome.code;
    if (outcome.code === DisconnectReason.loggedOut) {
      throw new Error("Session expired. Run setup again.");
    }

    if (
      outcome.code === DisconnectReason.restartRequired
      && attempt < maxConnectAttempts
    ) {
      continue;
    }

    throw new Error(`Disconnected (code: ${outcome.code || "unknown"}).`);
  }

  throw new Error(`Disconnected (code: ${lastCode || "unknown"}).`);
}

async function disconnect(sock: WASocket): Promise<void> {
  await new Promise((r) => setTimeout(r, 1500));
  sock.end(undefined);
}

// ─────────────────────────────────────────────────────────────────
// 2. SETUP — QR scan, find group, save config
// ─────────────────────────────────────────────────────────────────

async function setup(args: Record<string, string>): Promise<void> {
  const authDir = args["auth-dir"];
  const configPath = args["config-path"];
  const groupName = args["group-name"];
  const qrTimeout = parseInt(args["qr-timeout"] || "180000");

  if (!authDir || !configPath || !groupName) {
    console.error("Missing required args: --auth-dir, --config-path, --group-name");
    process.exit(1);
  }

  // Check if already set up
  if (fs.existsSync(configPath)) {
    try {
      const existing = JSON.parse(fs.readFileSync(configPath, "utf-8"));
      if (existing.group_jid && fs.existsSync(`${authDir}/creds.json`)) {
        console.log(`Already set up. Group: ${existing.group_name}`);
        return;
      }
    } catch {}
  }

  // Connect (show QR if no auth exists)
  const hasAuth = fs.existsSync(`${authDir}/creds.json`);
  if (!hasAuth) {
    console.log("");
    console.log("  1. Open WhatsApp on your phone");
    console.log("  2. Settings → Linked Devices → Link a Device");
    console.log("  3. Scan the QR code below");
    console.log("");
  }

  let sock: WASocket;
  try {
    sock = await connect(authDir, true, qrTimeout);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // If stored auth is stale/revoked, wipe only this auth folder and re-pair once.
    if (hasAuth && message.includes("Session expired")) {
      console.log("Stored session is invalid. Re-pairing with a fresh QR session...");
      fs.rmSync(authDir, { recursive: true, force: true });
      fs.mkdirSync(authDir, { recursive: true });
      console.log("");
      console.log("  1. Open WhatsApp on your phone");
      console.log("  2. Settings → Linked Devices → Link a Device");
      console.log("  3. Scan the new QR code below");
      console.log("");
      sock = await connect(authDir, true, qrTimeout);
    } else {
      throw err;
    }
  }
  console.log("Connected to WhatsApp.");

  // Find group
  console.log(`Looking for group "${groupName}"...`);
  const groups = await sock.groupFetchAllParticipating();
  const target = groupName.toLowerCase();

  let matchedJid: string | null = null;
  let matchedName: string | null = null;

  for (const [jid, meta] of Object.entries(groups)) {
    if (meta.subject?.toLowerCase() === target) {
      matchedJid = jid;
      matchedName = meta.subject;
      break;
    }
  }

  if (!matchedJid || !matchedName) {
    console.log(`Group "${groupName}" not found.`);
    console.log("Create it in WhatsApp, then run setup again.");
    console.log("(Your session is saved — no QR scan needed next time.)");
    await disconnect(sock);
    process.exit(1);
  }

  console.log(`Found group: ${matchedName}`);

  // Send welcome
  await sock.sendMessage(matchedJid, {
    text: `👋 *LedgerClaw connected!*\n\nThis group will receive your Daily Finance Briefing.`,
  });

  // Save config
  const config = {
    group_jid: matchedJid,
    group_name: matchedName,
    connected_at: new Date().toISOString(),
  };
  fs.mkdirSync(configPath.substring(0, configPath.lastIndexOf("/")), { recursive: true });
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2));

  await disconnect(sock);
  console.log("Setup complete.");
}

// ─────────────────────────────────────────────────────────────────
// 3. SEND — deliver one raw text message
// ─────────────────────────────────────────────────────────────────

async function send(args: Record<string, string>): Promise<void> {
  const authDir = args["auth-dir"];
  const groupJid = args["group-jid"];
  const timeout = parseInt(args["timeout"] || "30000");

  if (!authDir || !groupJid) {
    console.error("Missing required args: --auth-dir, --group-jid");
    process.exit(1);
  }

  // Read message from stdin
  const message = await new Promise<string>((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data.trim()));
  });

  if (!message) {
    console.error("No message received on stdin.");
    process.exit(1);
  }

  const sock = await connect(authDir, false, timeout);
  await sock.sendMessage(groupJid, { text: message });
  await disconnect(sock);
}

// ─────────────────────────────────────────────────────────────────
// 4. LISTEN — watch group, output JSON lines to stdout
// ─────────────────────────────────────────────────────────────────

async function listen(args: Record<string, string>): Promise<void> {
  const authDir = args["auth-dir"];
  const groupJid = args["group-jid"];

  if (!authDir || !groupJid) {
    console.error("Missing required args: --auth-dir, --group-jid");
    process.exit(1);
  }

  const sock = await connect(authDir, false, 30000);

  sock.ev.on("messages.upsert", ({ messages }) => {
    for (const msg of messages) {
      // Only messages from our target group
      if (msg.key.remoteJid !== groupJid) continue;
      // Skip messages sent by us
      if (msg.key.fromMe) continue;
      // Skip non-text messages
      const text = msg.message?.conversation
        || msg.message?.extendedTextMessage?.text;
      if (!text) continue;

      // Output as JSON line → Python bridge reads this
      const output = {
        from: msg.key.participant || msg.key.remoteJid,
        text: text,
        timestamp: new Date((msg.messageTimestamp as number) * 1000).toISOString(),
      };
      console.log(JSON.stringify(output));
    }
  });

  // Stay alive until killed
  await new Promise<void>((resolve) => {
    process.on("SIGINT", () => { disconnect(sock).then(resolve); });
    process.on("SIGTERM", () => { disconnect(sock).then(resolve); });
  });
}

// ─────────────────────────────────────────────────────────────────
// ROUTER — entry point
// ─────────────────────────────────────────────────────────────────

const args = parseArgs();

switch (args._command) {
  case "setup":  setup(args).catch((e) => { console.error(e.message); process.exit(1); }); break;
  case "send":   send(args).catch((e) => { console.error(e.message); process.exit(1); }); break;
  case "listen": listen(args).catch((e) => { console.error(e.message); process.exit(1); }); break;
  default:
    console.log("Usage:");
    console.log("  npx tsx whatsapp.ts setup  --auth-dir X --config-path X --group-name X");
    console.log("  npx tsx whatsapp.ts send   --auth-dir X --group-jid X --timeout X");
    console.log("  npx tsx whatsapp.ts listen --auth-dir X --group-jid X");
    break;
}
