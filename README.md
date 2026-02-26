# 🦅 LedgerClaw

Open-source, local-first personal finance intelligence agent.

**Gmail → Filter financial emails → Daily finance briefing → WhatsApp**

Your financial data never leaves your machine. LLM runs locally via Ollama (or optionally via cloud APIs). No accounts to create, no subscriptions, no data sharing.

---

## How It Works

Every day at your scheduled time (default 6 PM), LedgerClaw:

1. **Fetches** new emails from Gmail (incremental, only what's new)
2. **Filters** financial emails using your personalized rules (built organically from your actual inbox)
3. **Generates** a concise finance briefing using your chosen LLM
4. **Delivers** the briefing to your WhatsApp

If your laptop was off for a few days, LedgerClaw catches up automatically — you get one consolidated briefing covering all missed days.

---

## Prerequisites

- **Node.js** 20 or later
- **Ollama** (recommended) — [install from ollama.com](https://ollama.com)
  - Pull a model: `ollama pull llama3.2:7b`
- **Google Cloud project** with Gmail API enabled (free, takes 5 minutes)
- **WhatsApp** on your phone

---

## Quick Start

```bash
# Clone the repo
git clone https://github.com/your-org/ledgerclaw.git
cd ledgerclaw

# Install dependencies
npm install

# Run the guided onboarding
npm run onboard
```

The onboarding wizard walks you through:
1. Choosing your LLM provider (Ollama / Claude / OpenAI)
2. Setting your briefing time and timezone
3. Connecting your Gmail (OAuth2, read-only access)
4. Building your personalized finance filter
5. Linking your WhatsApp (QR code scan)
6. Generating your first briefing

After onboarding, start the scheduler:

```bash
# Start the daemon (runs in background, triggers at your scheduled time)
npm start

# Or for development with hot reload
npm run dev

# Or run the pipeline once manually
npm run run-pipeline
```

---

## Project Structure

```
~/.ledgerclaw/
├── config.json                  ← Schedule, timezone, settings
├── .env                         ← API keys (gitignored)
│
├── input/
│   ├── gmail/credentials.json   ← Google OAuth2 credentials
│   ├── gmail/token.json         ← Your Gmail access tokens
│   ├── rules/default.json       ← Default financial sender/keyword rules
│   ├── rules/user.json          ← Your personalized filter (auto-generated)
│   └── store/ledger.db          ← SQLite database
│
├── process/
│   ├── prompts/briefing.md      ← Briefing template (customisable!)
│   ├── prompts/classify-senders.md
│   ├── prompts/extract-keywords.md
│   └── models/config.json       ← LLM provider config
│
└── output/
    ├── whatsapp/auth/           ← WhatsApp session
    └── logs/pipeline.log        ← Run history
```

---

## Customisation

### Change briefing format or language

Edit `~/.ledgerclaw/process/prompts/briefing.md`. This is the prompt template sent to the LLM. You can:

- Change the language (Hindi, Tamil, Spanish, etc.)
- Change the format (actions first, table format, etc.)
- Add emphasis on specific categories (investments over transactions)
- Adjust the word limit

### Add or remove financial senders

Edit `~/.ledgerclaw/input/rules/user.json` to manually add or remove senders from the financial list. The filter automatically discovers new financial senders weekly, but you can override it.

### Change schedule

Edit `~/.ledgerclaw/config.json`:

```json
{
  "schedule": {
    "dailyBriefing": "0 18 * * *",
    "timezone": "Asia/Kolkata"
  }
}
```

Cron format: `minute hour * * *`. Examples:
- `"0 7 * * *"` → 7:00 AM
- `"30 18 * * *"` → 6:30 PM
- `"0 20 * * 1-5"` → 8 PM weekdays only

---

## Gmail Cloud Project Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project → name it "LedgerClaw"
3. Go to **APIs & Services** → **Library** → search "Gmail API" → **Enable**
4. Go to **APIs & Services** → **Credentials** → **Create Credentials** → **OAuth client ID**
5. Application type: **Desktop app** → name it "LedgerClaw"
6. Download the JSON file
7. Save it as `~/.ledgerclaw/input/gmail/credentials.json`

LedgerClaw only requests **read-only** access. It cannot send, delete, or modify your emails.

---

## Architecture

```
Scheduler → Briefer → Delivery Layer
                │
    ┌───────────┼────────────┐
    ▼           ▼            ▼
  Data       Filter     Intelligence
  Sources    Engine       Engine
  (Gmail)                    │
                   ┌─────────┼──────────┐
                   ▼         ▼          ▼
                LLM       Prompt      LLM
                Router    Creator    Interface
```

- **Pipeline, not agent** — steps are predetermined, not LLM-decided
- **Rules first, LLM fallback** — fast and free for 80-90% of emails
- **Organic filter growth** — your filter learns from your actual inbox
- **Single SQLite file** — all data in one place, easy to backup

---

## License

MIT
