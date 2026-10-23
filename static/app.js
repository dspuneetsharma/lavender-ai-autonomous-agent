"use strict";

// Example requests for the two demo scenarios (standard and complex).
const SAMPLES = {
  minutes: `Create meeting minutes for our weekly product sync held on Monday 14 October.
Attendees: Priya Shah (product lead), Omar Khan (engineering), Lena Ortiz (marketing).
Discussion: the mobile app launch is at risk because the payment SDK upgrade is not finished.
We decided to move the launch from 28 October to 4 November.
Omar will finish the payment SDK upgrade by 25 October.
Lena will update the launch announcement and press kit by 30 October.
Priya will brief the leadership team on the new date.
Please include a short summary, the decisions, and an action items table with owners and deadlines.`,
  plan: `Create a project plan for moving our sales team from spreadsheets to HubSpot CRM.
The budget is $40,000 and the go-live deadline is 31 March.
Priya Shah owns the data migration and Omar Khan leads the integration with our billing system.
The sales team has 25 users across two offices (London and Pune).
Include a short overview, the project phases with a timeline table, roles and responsibilities,
a training approach for the sales team, and success metrics.`,
};

const STATUS_ICONS = { done: "✓", failed: "✕", skipped: "–" };
const ELAPSED_TICK_MS = 1000;
const HTTP_NOT_A_DOCUMENT = 422;

// Small talk is answered here in the browser, so a greeting never costs an LLM call.
// A message counts as small talk only if every word is in these lists.
const SMALL_TALK_MAX_WORDS = 8;
const GREETING_WORDS = new Set([
  "hi", "hii", "hello", "hey", "heya", "hiya", "yo", "morning", "afternoon", "evening",
]);
const HOW_ARE_YOU_WORDS = new Set(["how", "whats", "sup", "wassup"]);
const THANKS_WORDS = new Set(["thanks", "thank", "thx", "ty"]);
const FILLER_WORDS = new Set([
  "there", "good", "are", "r", "you", "u", "ya", "doing", "today", "is", "it", "going", "up",
  "so", "much", "lavender", "again", "dear", "all", "ok", "okay", "great",
]);
const SMALL_TALK_REPLIES = {
  greeting: [
    "Hello! Can I help you with any document today?",
    "Hi there! Is there a document I can put together for you?",
    "Hey! Need any document today, such as meeting minutes, a project plan or a report?",
  ],
  howAreYou: [
    "I'm doing great, thanks for asking! Can I help you with any document today?",
    "All good here, thank you! Is there a document I can help you with?",
  ],
  thanks: [
    "You're welcome! Happy to help with another document anytime.",
    "Glad I could help! Just ask whenever you need another document.",
  ],
};
const OFF_TOPIC_REPLY =
  "I'd kindly request you to use me for documents, such as meeting minutes, project plans or reports. " +
  "For other tasks, Puneet will need to give me new skills first. Once that happens, " +
  "I'll be able to handle those tasks too.";

const chat = document.getElementById("chat");
const welcome = document.getElementById("welcome");
const input = document.getElementById("input");
const sendButton = document.getElementById("send");
const counter = document.getElementById("counter");
const status = document.getElementById("status");
const statusText = document.getElementById("status-text");

let maxChars = null;
let busy = false;

// ---------------------------------------------------------------- helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text; // never innerHTML: content is untrusted
  return node;
}

function appendMessage(role, child) {
  welcome.hidden = true;
  const row = el("div", `message ${role}`);
  row.append(child);
  chat.append(row);
  chat.scrollTop = chat.scrollHeight;
  return row;
}

function setStatus(state, text) {
  status.dataset.state = state;
  statusText.textContent = text;
}

function updateComposer() {
  const length = input.value.trim().length;
  const over = maxChars !== null && length > maxChars;
  counter.textContent = maxChars === null ? "" : `${length.toLocaleString()} / ${maxChars.toLocaleString()}`;
  counter.classList.toggle("over", over);
  sendButton.disabled = busy || length === 0 || over;
  input.style.height = "auto";
  input.style.height = `${input.scrollHeight}px`;
}

// ---------------------------------------------------------------- rendering

function renderPending() {
  const card = el("div", "card pending");
  const line = el("div");
  const started = Date.now();
  const update = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    line.replaceChildren(el("span", "spinner"), `Planning, writing and checking each section… ${seconds}s`);
  };
  update();
  card.append(line, el("div", "usage",
    "Short documents usually take about a minute. Larger ones can take several minutes " +
    "because of the free tier's rate limit."));
  const row = appendMessage("agent", card);
  const timer = setInterval(update, ELAPSED_TICK_MS);
  return () => { clearInterval(timer); row.remove(); };
}

function renderResult(data) {
  const card = el("div", "card");
  const head = el("div", "card-head");
  head.append(el("span", `badge ${data.status}`, data.status));
  if (data.title) head.append(el("span", "card-title", data.title));
  card.append(head, el("p", "card-message", data.message));

  if (data.document_url) {
    const link = el("a", "download", "Download Word document");
    link.href = data.document_url;
    link.setAttribute("download", "");
    card.append(link);
  }
  if (data.plan && data.plan.length) card.append(el("div", "section-label", "Plan"), renderPlan(data.plan));
  if (data.warnings && data.warnings.length) card.append(renderWarnings(data.warnings));
  if (data.usage) {
    const u = data.usage;
    card.append(el("div", "usage",
      `${u.llm_calls} LLM calls · ${u.tokens_used.toLocaleString()} tokens · ${u.duration_s}s` +
      (u.failed_attempts ? ` · ${u.failed_attempts} failed attempts` : "")));
  }
  appendMessage("agent", card);
}

function renderPlan(steps) {
  const list = el("ul", "plan");
  for (const step of steps) {
    const item = el("li", step.status);
    item.append(el("span", "icon", STATUS_ICONS[step.status] || "?"));
    const body = el("span", null, `${step.id} · ${step.title}`);
    if (step.detail) body.append(el("span", "detail", step.detail));
    item.append(body, el("span", step.tool === "code" ? "tool code" : "tool", step.tool));
    list.append(item);
  }
  return list;
}

function renderWarnings(warnings) {
  const details = el("details", "warnings");
  details.append(el("summary", null, `${warnings.length} warning${warnings.length === 1 ? "" : "s"}`));
  const list = el("ul");
  for (const warning of warnings) list.append(el("li", null, warning));
  details.append(list);
  return details;
}

function renderError(message) {
  const card = el("div", "card error");
  const head = el("div", "card-head");
  head.append(el("span", "badge failed", "error"));
  card.append(head, el("p", "card-message", message));
  appendMessage("agent", card);
}

function renderReply(text) {
  appendMessage("agent", el("div", "bubble", text));
}

function smallTalkKind(text) {
  const words = text.toLowerCase().replace(/['’]/g, "").split(/[^a-z]+/).filter(Boolean);
  if (words.length === 0 || words.length > SMALL_TALK_MAX_WORDS) return null;
  const known = (word) => GREETING_WORDS.has(word) || HOW_ARE_YOU_WORDS.has(word) ||
    THANKS_WORDS.has(word) || FILLER_WORDS.has(word);
  if (!words.every(known)) return null;
  if (words.some((word) => THANKS_WORDS.has(word))) return "thanks";
  if (words.some((word) => HOW_ARE_YOU_WORDS.has(word))) return "howAreYou";
  if (words.some((word) => GREETING_WORDS.has(word))) return "greeting";
  return null;
}

function pickOne(options) {
  return options[Math.floor(Math.random() * options.length)];
}

function validationMessage(body) {
  if (body && Array.isArray(body.detail) && body.detail.length) return body.detail[0].msg;
  if (body && typeof body.detail === "string") return body.detail;
  return "The server returned an unexpected response.";
}

// ---------------------------------------------------------------- actions

async function submit() {
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  appendMessage("user", el("div", "bubble", text));
  const smallTalk = smallTalkKind(text);
  if (smallTalk) {
    renderReply(pickOne(SMALL_TALK_REPLIES[smallTalk]));
    updateComposer();
    return;
  }
  busy = true;
  updateComposer();
  const stopPending = renderPending();
  try {
    const response = await fetch("/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: text }),
    });
    const body = await response.json();
    stopPending();
    // FastAPI's own 422 (invalid input) has no "status" field; the agent's "not a document" 422 does.
    if (response.status === HTTP_NOT_A_DOCUMENT && body && body.status) renderReply(OFF_TOPIC_REPLY);
    else if (body && body.status) renderResult(body);
    else renderError(validationMessage(body));
  } catch {
    stopPending();
    renderError("Could not reach the server. Is it still running?");
  } finally {
    busy = false;
    updateComposer();
    input.focus();
  }
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    const health = await response.json();
    maxChars = health.max_request_chars;
    setStatus("online", `Online · ${health.model}`);
  } catch {
    setStatus("offline", "Server unreachable");
  }
  updateComposer();
}

input.addEventListener("input", updateComposer);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!sendButton.disabled) submit();
  }
});
sendButton.addEventListener("click", submit);
for (const chip of document.querySelectorAll("[data-sample]")) {
  chip.addEventListener("click", () => {
    input.value = SAMPLES[chip.dataset.sample];
    updateComposer();
    input.focus();
  });
}

checkHealth();
