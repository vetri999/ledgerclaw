import fs from "fs";
import logger from "../utils/logger.js";

/**
 * Load a prompt template file and replace {{variable}} placeholders with values.
 */
export function buildPrompt(
  templatePath: string,
  variables: Record<string, string>
): string {
  if (!fs.existsSync(templatePath)) {
    throw new Error(`Prompt template not found: ${templatePath}`);
  }

  let prompt = fs.readFileSync(templatePath, "utf-8");

  for (const [key, value] of Object.entries(variables)) {
    prompt = prompt.replaceAll(`{{${key}}}`, value);
  }

  // Warn about unreplaced placeholders
  const unreplaced = prompt.match(/\{\{[a-zA-Z_]+\}\}/g);
  if (unreplaced) {
    logger.warn(`Unreplaced variables in prompt template: ${unreplaced.join(", ")}`);
  }

  return prompt;
}

/**
 * Format a list of emails grouped by category into a text block for the briefing prompt.
 */
export function formatEmailsForPrompt(
  emails: Array<{
    sender_name: string | null;
    sender: string;
    subject: string;
    body_text: string | null;
    category: string | null;
  }>
): string {
  // Group by category
  const grouped: Record<string, typeof emails> = {};
  for (const email of emails) {
    const cat = email.category || "other_financial";
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(email);
  }

  const categoryNames: Record<string, string> = {
    credit_card: "Credit Card",
    bank_alert: "Banking",
    loan: "Loans & EMIs",
    investment: "Investments",
    insurance: "Insurance",
    tax: "Tax",
    salary: "Salary",
    payment_app: "Payment Apps",
    other_financial: "Other Financial",
  };

  let output = "";
  for (const [cat, catEmails] of Object.entries(grouped)) {
    const name = categoryNames[cat] || cat;
    output += `\n### ${name}\n`;
    for (const e of catEmails) {
      const from = e.sender_name || e.sender;
      const body = (e.body_text || "").substring(0, 300).trim();
      output += `- From: ${from}\n  Subject: ${e.subject}\n`;
      if (body) {
        output += `  Body: ${body}\n`;
      }
      output += "\n";
    }
  }

  return output.trim();
}
