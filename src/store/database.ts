import Database from "better-sqlite3";
import { PATHS } from "../utils/paths.js";
import logger from "../utils/logger.js";
import fs from "fs";

let _db: Database.Database;

const SCHEMA = `
  CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT,
    sender          TEXT NOT NULL,
    sender_name     TEXT,
    subject         TEXT NOT NULL,
    body_text       TEXT,
    received_at     INTEGER NOT NULL,
    fetched_at      INTEGER NOT NULL,
    labels          TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
  CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender);

  CREATE TABLE IF NOT EXISTS classifications (
    email_id        TEXT PRIMARY KEY REFERENCES emails(id),
    is_financial    INTEGER NOT NULL,
    category        TEXT,
    confidence      REAL NOT NULL,
    classified_by   TEXT NOT NULL,
    classified_at   INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS briefings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at    INTEGER NOT NULL,
    period_start    INTEGER NOT NULL,
    period_end      INTEGER NOT NULL,
    email_count     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    model_used      TEXT,
    tokens_used     INTEGER,
    delivered       INTEGER NOT NULL DEFAULT 0,
    delivered_via   TEXT,
    delivered_at    INTEGER
  );

  CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_id     INTEGER NOT NULL REFERENCES briefings(id),
    email_id        TEXT REFERENCES emails(id),
    description     TEXT NOT NULL,
    due_date        TEXT,
    priority        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
  CREATE INDEX IF NOT EXISTS idx_actions_due ON actions(due_date);

  CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        INTEGER NOT NULL,
    finished_at       INTEGER,
    status            TEXT NOT NULL,
    trigger           TEXT NOT NULL,
    period_start      INTEGER,
    period_end        INTEGER,
    emails_fetched    INTEGER DEFAULT 0,
    emails_classified INTEGER DEFAULT 0,
    emails_financial  INTEGER DEFAULT 0,
    tokens_used       INTEGER DEFAULT 0,
    briefing_id       INTEGER REFERENCES briefings(id),
    error             TEXT
  );

  CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_history_id TEXT,
    last_fetched_at INTEGER,
    last_message_ts INTEGER
  );
`;

// ─── Init ───────────────────────────────────────────────────────────

export function initDatabase(): Database.Database {
  fs.mkdirSync(PATHS.storeDir, { recursive: true });
  _db = new Database(PATHS.database);
  _db.pragma("journal_mode = WAL");
  _db.pragma("foreign_keys = ON");
  _db.exec(SCHEMA);
  logger.info("Database initialized");
  return _db;
}

export function getDb(): Database.Database {
  if (!_db) return initDatabase();
  return _db;
}

// ─── Emails ─────────────────────────────────────────────────────────

export interface EmailRow {
  id: string;
  thread_id: string | null;
  sender: string;
  sender_name: string | null;
  subject: string;
  body_text: string | null;
  received_at: number;
  fetched_at: number;
  labels: string | null;
}

export function insertEmail(email: EmailRow): void {
  getDb()
    .prepare(
      `INSERT OR IGNORE INTO emails (id, thread_id, sender, sender_name, subject, body_text, received_at, fetched_at, labels)
       VALUES (@id, @thread_id, @sender, @sender_name, @subject, @body_text, @received_at, @fetched_at, @labels)`
    )
    .run(email);
}

export function emailExists(id: string): boolean {
  return !!getDb().prepare("SELECT 1 FROM emails WHERE id = ?").get(id);
}

export function getEmailsByIds(ids: string[]): EmailRow[] {
  if (ids.length === 0) return [];
  const ph = ids.map(() => "?").join(",");
  return getDb()
    .prepare(`SELECT * FROM emails WHERE id IN (${ph}) ORDER BY received_at DESC`)
    .all(...ids) as EmailRow[];
}

export function getEmailsSince(sinceTs: number): EmailRow[] {
  return getDb()
    .prepare("SELECT * FROM emails WHERE received_at >= ? ORDER BY received_at DESC")
    .all(sinceTs) as EmailRow[];
}

export function getAllUniqueSenders(): string[] {
  return (
    getDb().prepare("SELECT DISTINCT sender FROM emails").all() as { sender: string }[]
  ).map((r) => r.sender);
}

export function getSubjectsBySenders(senders: string[]): string[] {
  if (senders.length === 0) return [];
  const ph = senders.map(() => "?").join(",");
  return (
    getDb()
      .prepare(`SELECT DISTINCT subject FROM emails WHERE sender IN (${ph})`)
      .all(...senders) as { subject: string }[]
  ).map((r) => r.subject);
}

export function getEmailCount(): number {
  return (getDb().prepare("SELECT COUNT(*) as c FROM emails").get() as { c: number }).c;
}

// ─── Classifications ────────────────────────────────────────────────

export interface ClassificationRow {
  email_id: string;
  is_financial: number;
  category: string | null;
  confidence: number;
  classified_by: string;
  classified_at: number;
}

export function insertClassification(c: ClassificationRow): void {
  getDb()
    .prepare(
      `INSERT OR REPLACE INTO classifications (email_id, is_financial, category, confidence, classified_by, classified_at)
       VALUES (@email_id, @is_financial, @category, @confidence, @classified_by, @classified_at)`
    )
    .run(c);
}

export function getUnclassifiedEmailIds(): string[] {
  return (
    getDb()
      .prepare(
        "SELECT e.id FROM emails e LEFT JOIN classifications c ON e.id = c.email_id WHERE c.email_id IS NULL"
      )
      .all() as { id: string }[]
  ).map((r) => r.id);
}

export function getFinancialEmailIdsSince(sinceTs: number): string[] {
  return (
    getDb()
      .prepare(
        `SELECT e.id FROM emails e JOIN classifications c ON e.id = c.email_id
         WHERE c.is_financial = 1 AND e.received_at >= ? ORDER BY e.received_at DESC`
      )
      .all(sinceTs) as { id: string }[]
  ).map((r) => r.id);
}

export function getClassification(emailId: string): ClassificationRow | undefined {
  return getDb()
    .prepare("SELECT * FROM classifications WHERE email_id = ?")
    .get(emailId) as ClassificationRow | undefined;
}

// ─── Briefings ──────────────────────────────────────────────────────

export function insertBriefing(b: {
  generated_at: number;
  period_start: number;
  period_end: number;
  email_count: number;
  content: string;
  model_used: string | null;
  tokens_used: number;
}): number {
  const r = getDb()
    .prepare(
      `INSERT INTO briefings (generated_at, period_start, period_end, email_count, content, model_used, tokens_used)
       VALUES (@generated_at, @period_start, @period_end, @email_count, @content, @model_used, @tokens_used)`
    )
    .run(b);
  return Number(r.lastInsertRowid);
}

export function markBriefingDelivered(id: number, channel: string): void {
  getDb()
    .prepare("UPDATE briefings SET delivered = 1, delivered_via = ?, delivered_at = ? WHERE id = ?")
    .run(channel, Date.now(), id);
}

export function markBriefingFailed(id: number): void {
  getDb().prepare("UPDATE briefings SET delivered = -1 WHERE id = ?").run(id);
}

export function getBriefing(id: number): any {
  return getDb().prepare("SELECT * FROM briefings WHERE id = ?").get(id);
}

// ─── Actions ────────────────────────────────────────────────────────

export function insertAction(a: {
  briefing_id: number;
  email_id: string | null;
  description: string;
  due_date: string | null;
  priority: string;
  created_at: number;
}): void {
  getDb()
    .prepare(
      `INSERT INTO actions (briefing_id, email_id, description, due_date, priority, created_at)
       VALUES (@briefing_id, @email_id, @description, @due_date, @priority, @created_at)`
    )
    .run(a);
}

// ─── Pipeline Runs ──────────────────────────────────────────────────

export function createPipelineRun(trigger: string): number {
  const r = getDb()
    .prepare("INSERT INTO pipeline_runs (started_at, status, trigger) VALUES (?, 'running', ?)")
    .run(Date.now(), trigger);
  return Number(r.lastInsertRowid);
}

export function updatePipelineRun(id: number, updates: Record<string, any>): void {
  const keys = Object.keys(updates);
  const sets = keys.map((k) => `${k} = @${k}`).join(", ");
  getDb()
    .prepare(`UPDATE pipeline_runs SET ${sets} WHERE id = @_id`)
    .run({ ...updates, _id: id });
}

export function getLastSuccessfulRun(): any {
  return getDb()
    .prepare("SELECT * FROM pipeline_runs WHERE status IN ('success','skipped') ORDER BY finished_at DESC LIMIT 1")
    .get();
}

// ─── Sync State ─────────────────────────────────────────────────────

export function getSyncState(source: string): any {
  return getDb().prepare("SELECT * FROM sync_state WHERE source = ?").get(source);
}

export function upsertSyncState(state: {
  source: string;
  last_history_id: string | null;
  last_fetched_at: number;
  last_message_ts: number | null;
}): void {
  getDb()
    .prepare(
      `INSERT INTO sync_state (source, last_history_id, last_fetched_at, last_message_ts)
       VALUES (@source, @last_history_id, @last_fetched_at, @last_message_ts)
       ON CONFLICT(source) DO UPDATE SET
         last_history_id = @last_history_id, last_fetched_at = @last_fetched_at, last_message_ts = @last_message_ts`
    )
    .run(state);
}
