# Decisions Log

Engineering notes, one entry per build step: the key decision, why, the rejected alternative, and a common design question with its answer.

## Tech stack in one line each

| Tool | What it is | Why here |
|---|---|---|
| **FastAPI** | Python web framework for building APIs | Validates requests via type hints and gives free Swagger docs at `/docs` |
| **Uvicorn** | The server that runs a FastAPI app | Standard, lightweight ASGI server for local runs |
| **Pydantic** | Data validation using Python classes | Every LLM JSON output and API body is checked against a typed model |
| **pydantic-settings** | Pydantic for config | Reads `.env` into one typed, validated `Settings` object |
| **Groq** (`openai/gpt-oss-120b`) | Hosted LLM API with a free tier | Strong open model, fast inference, no cost |
| **python-docx** | Library to create Word files in Python | The `.docx` is built locally; the LLM only returns text |
| **pytest** | Python test runner | Simple tests, with a fake LLM so no API calls are needed |

---

## Step 1: Setup

- **Decision:** All budgets, limits, paths and the API key live in one typed `Settings` class loaded from `.env`. It checks itself at startup (e.g. per-call cap must not exceed the per-minute window).
- **Why:** No magic numbers in code; tuning is a config change. Bad configs fail at startup, not halfway through a request.
- **Rejected:** Constants scattered in modules, or raw `os.getenv` calls (untyped, unchecked, easy to typo).
- **Also:** Exact pinned versions in `requirements.txt` so a reviewer installs the same environment; `.env` is git-ignored and `.env.example` documents every key.

> **Q: Why is the API key optional in `Settings`?**
> A: Tests run with a fake LLM and must not need a real key. The Groq client checks for the key itself and fails with a clear message if it is missing.

### Layout adjustment (approved after Step 1)

Same functionality and scope as the plan; only the file split changes.

```
app/
  main.py          API routes
  config.py        settings and logging
  schemas.py       Pydantic models
  llm.py           Groq wrapper, token pacer, FakeLLM
  guardrails.py    input checks, plan validation
  grounding.py     record-vs-proposal grounding, completeness gate
  tools.py         tool registry
  agent/
    planner.py     analyze + plan (with one re-plan)
    executor.py    run tasks in dependency order
    review.py      chunked review, repair, verify
    pipeline.py    orchestrates the steps and applies the success/partial/failed rules
  docx_builder.py  render + reopen-verify
```

- **Decision:** Split `agent.py` into an `agent/` package, and move grounding and completeness out of `guardrails.py` into `grounding.py`.
- **Why:** Each file has one job and stays small. Input and plan checks run *before* the LLM writes anything; grounding runs *after* on what it wrote. They are different stages with different tests.
- **Rejected:** One large `agent.py` (hard to read and review) and one `guardrails.py` mixing pre- and post-generation checks.

> **Q: Where does the success/partial/failed decision live?**
> A: Only in `agent/pipeline.py`. The other modules report facts (task failed, item removed, issue unresolved), and the pipeline applies the rules in one place.

---

## Step 2: LLM wrapper (`app/llm.py`)

- **Decision:** One `LLMClient.chat_json()` entry point, sitting on a swappable **transport** (`GroqTransport` for real calls, `FakeLLM` for tests). The client owns every safety rule: per-call cap check, token ceiling, deadline, pacer wait, 429 handling, schema retries and the call ledger.
- **Why:** Tests swap only the network part, so they exercise the *real* pacing and retry code. No rule is duplicated in the agent.
- **Rejected:** Mocking the Groq SDK directly (tests would skip our own logic) and the SDK's built-in retries (set `max_retries=0`). Built-in retries would ignore our budget and deadline.

How it behaves:
- **Errors** fall into three types the agent acts on: `BudgetExhausted` (call not made → task skipped), `ContextTooLarge` (never resent) and `LLMCallFailed` (retries used up).
- **Pacer** is a process-wide sliding 60 s window, capped at 7.5K tokens. Calls book their slot *before* waiting, so two parallel requests can't overbook. After each call, the booking is corrected to the real `usage`.
- **Unknown usage** (timeout, 5xx) keeps the full reservation charged. Being conservative here means we never undercount.
- **Schema** is attached to the system prompt by the client, so it is always included in the token estimate.
- **Retries:** invalid JSON or truncated output gets one feedback message ("your reply was rejected: …") and counts as an attempt, up to 2 retries. A 429 waits for `retry-after` but does not use up an attempt (max 3 waits).
- **Two small config additions:**
  - `GROQ_REASONING_EFFORT=low`: gpt-oss reasoning tokens count toward the 2K completion cap, and "low" keeps truncation rare.
  - `LLM_RATE_LIMIT_FALLBACK_WAIT_S`: used only if a 429 has no `retry-after` header.

> **Q: Why reserve tokens before the call instead of counting after?**
> A: The 8K/min limit is enforced by Groq on what we *send*. If we only counted afterwards, a burst of calls could hit 429s first. Reserving the estimate plus the completion cap up front, then correcting to real usage, keeps us under the limit with no wasted calls.

---

## Step 3: Schemas (`app/schemas.py`)

- **Decision:** Every LLM output is a strict Pydantic model: `Analysis` (with the task list), `Plan`, `SectionContent`, `TableContent` and `ReviewResult`. Bullets and table rows share one `Item` model, which has fact fields (`owner`, `due`, `value`), a `kind` (`record`/`proposal`), a `source_quote` and a `label`.
- **Why:** If a reply doesn't match its schema, the call is retried with the exact error. Because items are structured, grounding can check each fact in code instead of re-reading prose.
- **Rejected:** Free-text LLM output parsed with regex (fragile). Separate row models per table type (more code, and grounding would need one rule per table).

Details:
- **`source_quote` on record items:** the LLM paraphrases, so checking a whole decision sentence word-for-word against the request would almost always fail. Instead, the item carries the verbatim request fragment that supports it, and code checks that fragment. Short fields (owner, date, figure) are checked directly.
- **ID patterns** (`R1`, `A1`, `T1`) are enforced by the schema, which keeps cross-references checkable.
- **`tool` is a plain string, not an enum:** an unknown tool must reach plan validation and trigger the one re-plan with feedback, not just a schema retry.
- **Schema size:** Pydantic's auto-generated `"title"` labels are stripped before sending (Analysis ~1.56K → ~1.3K tokens).
- **Known limit:** the analyze call reserves ~1.3K schema + 2.5K completion + instructions + the request, so requests longer than about 4,000–4,500 chars hit the 6.5K per-call cap and fail clearly with "context too large". The plan accepts this; the README will say so.

> **Q: How does the agent tell a real decision from an invented one?**
> A: A decision in meeting minutes is a `record` item and must carry a `source_quote` that appears in the user's request. Code checks the quote; if it isn't there, the item is removed with a warning. The LLM never decides whether its own output is grounded.

---

## Step 4: Guardrails (headline improvement)

Four layers, all plain Python, so the LLM never grades its own work:

| Layer | File | When | Effect |
|---|---|---|---|
| Input checks | `schemas.py` + `guardrails.py` | before any LLM call | Empty or too long → 422. Injection or unsafe keyword → 400. Zero tokens spent |
| Plan validation | `guardrails.py` | after analyze | Unknown tool, duplicate id, missing dependency, cycle, 2–8 tasks, unknown or uncovered requested item → one re-plan with the exact errors, then fail |
| Grounding | `grounding.py` | after tasks run | Record facts not in the request → "To be confirmed"; unsupported records removed (never relabelled as proposals); labelled proposals kept |
| Completeness + gate | `grounding.py` | before rendering | Empty required section → missing-source note (partial) or fail; unlabelled proposal, uncovered item or placeholder text → fail |

- **Decision:** In record documents (minutes), code forces decisions, actions, attendees and figures to `record`, and every owner, date and figure is checked. In planning documents the claimed kind is kept, but a claimed `record` must pass grounding. If it isn't supported, it's removed or its field becomes TBC; it is **never** downgraded to a proposal. Generated content must be a labelled `proposal`, which the gate checks.
- **Why:** The LLM's own label is what we're trying to verify, so it can't be what we trust. Plans also hold real facts (an assigned owner, an agreed deadline, an existing budget). Those must stay grounded records, not be relabelled "Proposed".
- **Fix made before Step 5:** the first version forced every task, role and milestone in a plan to `proposal`. That would have shown "Priya owns the data migration, due March 31" as "Proposed" and skipped checking its date. Covered by `tests/test_grounding.py`:
  - supported assignment → kept;
  - invented deadline on a valid quote → that field becomes TBC;
  - unsupported claimed assignment → removed;
  - labelled proposal → kept and passes the gate.
- **Each field is checked on its own:** a matching `source_quote` only proves the item exists. Owner, due and value must each appear in the request themselves.
- **Rejected:**
  - An LLM "fact-check" call: it costs tokens, and it's the same model judging itself.
  - Fuzzy or semantic matching: harder to explain and to test. Substring matching is strict and predictable. A real date written in another format becomes TBC, which is the safe failure.
- **Also:**
  - `guardrails.py` only reports problems (a list of errors) and `grounding.py` returns results objects. Neither decides the request status; `agent/pipeline.py` does.
  - A section that *had* rows and lost them all counts as empty even if an intro sentence remains, so "The following actions were agreed:" followed by nothing can't pass as content.
  - These are limited safeguards: keyword screens catch obvious attempts only. The README will say so.

> **Q: What happens if the LLM invents an owner for an action item in meeting minutes?**
> A: Grounding checks the owner against the request text. If the name isn't there, the owner becomes "To be confirmed" and a warning is returned to the user. If the whole action isn't supported by a quote from the request, it's removed. If that empties a required section, the section either gets an explicit "to be confirmed" note (when the request genuinely had no such facts, giving status partial) or the request fails.

---

## Step 5: Agent (`app/tools.py`, `app/agent/`)

Flow per request:
1. `screen_request` (0 calls)
2. **analyze** (1 call, returns the analysis and the drafted plan)
3. **validate plan**, with at most one **re-plan**
4. **execute** tasks (1 call each)
5. **ground** (code)
6. **completeness** (code)
7. **review** in chunks
8. **repair** (≤3 sections) + re-ground + **verify**
9. **gate** (code)
10. **render**

| File | Job |
|---|---|
| `tools.py` | Registry of the only two tools (`write_section`, `build_table`): typed output schema, instructions, converter to `Section` |
| `agent/prompting.py` | Shared prompt blocks (request fenced in `<request>` tags, requested items, assumptions, section JSON) |
| `agent/planner.py` | analyze + validate + one re-plan; code sets `required` |
| `agent/executor.py` | Runs tasks in dependency order; a skipped prerequisite skips the task; stops early after a required failure |
| `agent/review.py` | Packs sections into chunks that each fit the 6.5K cap, then repairs, re-grounds and verifies |
| `agent/pipeline.py` | The only place that decides success / partial / failed and the HTTP code |

- **Decision:** One LLM call per task. Each task receives the **full validated output** of the tasks it depends on, but only the **titles** of the others.
- **Why:** Small calls fit the 8K/min limit, each output is validated on its own, and a failure is pinned to one task.
- **Rejected:** One big "write the whole document" call: too large for the per-call cap, and one bad section would mean re-generating everything.
- **Context trimming:** the request, requested items, missing info, assumptions and prerequisites are essential. Only the outline of other sections may be dropped to fit the cap; if it still doesn't fit, the task fails with "context too large".
- **HTTP codes:** 200 success/partial, 400 input screen, 422 not a document request (after its single analysis call), 502 failed.
- **The response's plan list** shows the LLM's tasks plus the code steps (ground, review, repair, gate, render), each with its status, so a reviewer can see exactly where a request stopped.

Fixed while smoke-testing:
- **Execution follows document order** when dependencies allow (T1, T2, T3), instead of running every ready task in "waves".
- **LLM-facing models reject unknown fields** (`extra="forbid"`). Before this, a table-shaped reply sent to a `write_section` task was silently accepted as *empty* content. Now it triggers the precise retry. Cost: about +3% schema tokens.

Live connection test: one call through the real client returned valid JSON (200 OK), using 209 real tokens against a 474-token reservation, in 0.8 s.

### Live dry run and prompt tweaks

One live end-to-end run of standard meeting minutes, with a stub renderer:
- **Result:** success, 4 calls, 6,166 tokens, 60.8 s (55.5 s of it pacer wait), no warnings.
- **Grounding:** every decision and action came back as a `record` with an exact quote. The model wrote "To be confirmed" itself for a deadline the request didn't give.
- **Two quality gaps, fixed with prompt-only edits to `planner.py`** (no logic change):
  1. **No Attendees section.** The planner covered only what was explicitly asked. It now also adds the *standard sections for the document type*, but only when the request supplies their content, as `required=false` tasks. These are still grounded, and as optional sections they can never fail a request.
  2. **Trivial assumptions** (e.g. "location not required"). The planner now records only choices that change the content, and leaves the list empty otherwise.
- **Why prompt, not code:** these are quality preferences, not correctness rules. Correctness stays enforced in code (grounding, gate). A missing optional section isn't an error, so a code check would be the wrong tool.
- **Effect on size:** the analyze call now fits requests up to about 4,300 characters within the 6.5K per-call cap (measured locally; an estimate, not a guarantee).

> **Q: Why was the run 61 s when the calls took 5 s?**
> A: The pacer reserves each call's full completion cap ahead of time to stay under 8K tokens per minute. Real completions were smaller, so it waited for the first call to leave the 60 s window. It's a deliberate trade: slightly slower, never rate-limited, still well inside the 300 s deadline.

> **Q: Why does the pipeline stop after a required task fails?**
> A: The request is already going to fail, because a required section can't be produced. Running the remaining tasks, plus review, would only burn tokens from the 30K budget and the daily quota. They are reported as "skipped" with the reason, so the user still sees the full plan.

---

## Step 6: Word document builder (`app/docx_builder.py`)

- **Decision:** The `.docx` is built locally with python-docx from grounded `Section` objects. The LLM only ever returns JSON text. Layout:
  - title and a "type · date" line;
  - a legend line when any proposal is present;
  - one heading per section, with paragraphs, bullet items and tables (teal header row);
  - an **Assumptions** section, always present;
  - a footer.
- **Why:** The file format is deterministic and safe: no model-generated markup and no prompt-injected formatting. Grounding has already run on the exact data that gets rendered.
- **Rejected:** Asking the LLM for Markdown or HTML and converting it (formatting drifts, harder to verify), and Word templates (extra moving parts for no grading benefit).

Visible honesty in the document:
- Proposals carry a bold tag ("Proposed", "Recommended", "Assumption A1"). In tables, a **Basis** column appears whenever any row is a proposal, and record rows say "Recorded".
- "To be confirmed" values and missing-source notes are rendered exactly as they are, never hidden.

Safety and reliability:
- **File name:** `<uuid4 hex>-<ascii slug>.docx`. `is_safe_document_name()` accepts only that exact pattern, so `../secret` or `x.docx.exe` can never be served.
- **Atomic write:** save to a temp file in the same folder → reopen and verify (title matches; heading count = sections + Assumptions) → `os.replace` into place. If verification fails, the temp file is deleted and `RenderError` is raised, so a half-written or wrong file never gets a link.
- The pipeline catches `RenderError` specifically (not bare `Exception`): expected render problems become a clean "failed", while a genuine bug still surfaces as a 500 instead of being disguised.

> **Q: Why reopen the file you just wrote?**
> A: Saving without an error doesn't prove the file is right. Reopening with the same library checks that Word-readable content is there: the title and every section heading, including Assumptions. If it isn't, the user gets a clear failure instead of a broken download.

### Add-on: potential risks in planning documents

- **Decision:** In plans and proposals, the agent may add an optional "Potential Risks & Suggested Mitigations" section built from the request's context. Risks are `proposal` items labelled **Potential**; mitigations are worded as recommendations. In minutes, only risks actually discussed are recorded, unless the user asks for recommendations.
- **Changes (small, no grounding rule changed):**
  - `ProposalLabel` gains `"Potential"`, and the document legend mentions it.
  - Analyze prompt: `missing_info` is only for facts the user must supply (names, owners, dates, budgets, attendees), not for content the agent can reasonably propose (risks, mitigations). This fixes the real gap: before, missing risks became "To be confirmed" instead of a useful assessment.
  - Planner may add the risks section (`required=false` unless the user asked for risks, so it can never fail a request).
  - Content rules: never word a potential risk as a confirmed problem, and never invent company-specific facts, statistics or probabilities.
  - Record-document guidance: only risks actually discussed.
- **Focused check:** `test_potential_risk_is_kept_and_rendered_with_its_label`. A Potential risk in a plan survives grounding unchanged, passes the gate and renders as "Potential: …". The *prompt* behaviour can only be confirmed by a live run (Step 10), because FakeLLM replays scripted replies.
- **Cost:** about +140 tokens on the analyze prompt (more than first estimated). The analyze call now fits requests up to about **3,990 characters** within the 6.5K per-call cap, down from about 4,300.

> **Q: Isn't generating risks the user never mentioned "inventing content"?**
> A: Not if it's honest about what it is. Risks are proposals labelled "Potential", and the legend says labelled items are possibilities, not facts. Code still blocks invented records: names, dates, figures and decisions must come from the request. In minutes, which document what happened, risks are limited to what was discussed.

---

## Step 7: API (`app/main.py`)

| Route | Purpose |
|---|---|
| `POST /agent` | Body `{"request": "..."}` → status, message, plan with per-step status, warnings, usage, `document_url` |
| `GET /documents/{name}` | Downloads a generated `.docx` (Word MIME type, friendly file name without the uuid) |
| `GET /health` | Liveness and model name; no LLM call |
| `GET /docs` | Swagger UI, generated by FastAPI |

- **Decision:** An **app factory** `create_app(settings, pipeline)`. The real Groq client is built in the startup hook, not at import. Tests pass a FakeLLM-backed pipeline instead.
- **Why:** Importing the app or running tests never needs a key or the network, and the production path is exactly the same code.
- **Rejected:** A module-level global client (import would fail without a key, and tests would need monkeypatching).
- **`POST /agent` is a plain `def`:** the agent blocks (LLM calls, pacer sleeps), so FastAPI runs it in its threadpool and the server stays responsive. The pacer is thread-safe.
- **HTTP codes come from the pipeline:** 200 success/partial, 400 input screen, 422 bad body or not a document, 502 agent failure. A catch-all handler turns real bugs into a logged 500 with a generic message, never a stack trace.
- **Download safety:** three checks before serving:
  1. the name must match the generated pattern;
  2. the resolved path must sit directly inside `outputs/`;
  3. the file must exist.
  Anything else is 404, which doesn't reveal whether a file exists. Tested with `../../.env`, backslash traversal, wrong suffix and unknown names.
- **Checked live:** uvicorn started with the real `.env` ("Agent ready"), and `/health` and `/docs` returned 200. No `/agent` call was made.
- **Open item for Step 9:** Starlette warns that its TestClient wants `httpx2`. It affects tests only; Groq's SDK still needs `httpx`.

> **Q: Why return 404 instead of 400 for an unsafe file name?**
> A: A 400 tells an attacker "your traversal attempt was noticed and parsed". A 404 gives no information beyond "not here". Valid names are 32 random hex characters plus a slug, so they can't be guessed either.

---

## Step 8: Chat UI (`static/`)

- **Decision:** One static page (`index.html`, `style.css`, `app.js`) in plain HTML/CSS/JS, served by FastAPI at `/` after the API routes. It's a chat, not a form: one text box (Enter sends, Shift+Enter adds a new line), two example chips (standard minutes, complex project plan) and a dark theme with a teal accent.
- **Why:** No build step, no framework and no extra server, so a reviewer runs one command and opens `localhost:8000`. The API stays the product; the UI is a thin client over `POST /agent`.
- **Rejected:** React/Vite (a build toolchain for one screen), Streamlit/Gradio (a second server and a different look), and a form with separate fields (the product takes a free-text request).

What each reply card shows:
- the status badge, title and message;
- a **Download Word document** button (success/partial only);
- the plan, with ✓ / ✕ / – per step (LLM tasks and code steps, each with its tool tag and failure reason);
- collapsible warnings, and usage (calls, tokens, seconds, failed attempts).
While a request runs, a spinner shows the elapsed seconds and explains that rate-limit pacing can take a minute or more.

Small but deliberate:
- **No `innerHTML`:** every server or LLM string goes through `textContent`, so a request or model output containing HTML can't inject script into the page.
- **The counter uses the server's limit:** `/health` now returns `max_request_chars`, so the UI never carries its own copy of 6,000. Send is disabled when the box is empty, too long, or a request is running.
- **All error shapes handled:** agent results (200/400/422/502), FastAPI validation errors (`detail` list) and network failure each get a clear card.
- Accessible basics: `aria-live` on the chat, labelled controls, visible focus rings, reduced-motion support and a mobile layout.

**Verified:** page, CSS and JS served (200), `app.js` passes `node --check`, and API checks and traversal 404s are unchanged with the UI mounted at `/`. Claude in Chrome wasn't connected, so the visual check is left to the user on a FakeLLM preview server (port 8001, no tokens), with a real-model check in Step 10.

> **Q: Why does the UI fetch the character limit from `/health` instead of hard-coding 6000?**
> A: One source of truth. If the limit changes in `.env`, the server and the UI agree automatically. A copied constant would silently drift, and the UI would let users type requests the server then rejects.

### UI add-on: Lavender name, greetings and off-topic replies (user request, UI only)

- **Decision:** The UI is named **Lavender**, with the welcome line "Your document generating assistant. What can I do for you?". The backend, Swagger title and Word footer are unchanged. Short small-talk messages (greetings, "how are you", thanks) get a natural reply chosen in the browser. Any other non-document request still goes to the agent; when it returns its "not a document request" 422, the UI shows a polite reply asking the user to use it for documents and explaining that Puneet would need to add new skills for anything else.
- **Why:** Small talk is answered without calling Groq, which saves one call of about 3–5K tokens per greeting. Off-topic detection stays with the LLM, which understands meaning; a keyword list can't. All replies are fixed text, so they read naturally and can't be changed by a prompt injection.
- **Rejected:** a chat mode in the LLM (needs a second prompt path and conversation memory, both outside the plan), and keyword-based off-topic detection (would wrongly reject real document requests).
- **Safety of the greeting check:** a message counts as small talk only if it has 8 words or fewer and *every* word is on a short list, so "hi, write meeting minutes for Monday" still goes to the agent. Checked with 12 cases in a Node script; `node --check` passes.

> **Q: Why is a greeting handled in the browser but an off-topic question handled by the LLM?**
> A: A greeting is a tiny, closed set of words, so an exact word list is safe and free. "Off-topic" is open-ended and needs meaning, so the LLM decides, and the UI only turns its answer into a friendly reply.
- **Icon:** a hand-drawn SVG lavender sprig (`static/lavender.svg`) used as the favicon, the top-bar logo and the welcome image. It's a small text file, so there are no image or licence concerns and it stays sharp at any size. The rest of the theme stays teal.

## Fix: budget limits sized for the Groq free tier

**What happened (live run):** a complex CRM cloud migration request produced an 8-section plan. After the plan call, T1–T6 and one failed attempt (8 LLM calls, 18,064 tokens, 242 s), T7 couldn't start because the 300 s deadline minus the 60 s call timeout left no room. T7 was required (the user asked for risks), so the run correctly failed, but only after spending most of the budget.

**Root cause:** the limits didn't match the rate limit. Each call reserves its prompt plus the full 2,000-token reply cap, so with a 7,500-token window the pacer allows about **two section calls per minute** in practice. An offline simulation with the real `TokenPacer` and a fake clock (no API calls) reproduced the stop near task 7. It estimated that 8 sections plus a review need about 33K tokens and about 7 minutes, more than 30K and 300 s.

- **Decision:**
  - `MAX_PLAN_TASKS` 8 → **6**, and the planner (and re-planner) groups closely related requested items into one section (e.g. risks with mitigations, decisions with the changes required) and lists every grouped item in `covers`;
  - `REQUEST_DEADLINE_S` 300 → **480**;
  - `REQUEST_TOKEN_CEILING` 30,000 → **40,000** (a hard maximum, not expected usage);
  - `LLM_CALL_TIMEOUT_S` 60 → **30** (real calls take about 5–15 s);
  - the UI waiting message now says larger documents can take several minutes.
- **Why:** an oversized plan is now rejected by the existing plan check and grouped by one re-plan **before any section is written**, instead of failing after four minutes. Plan validation still rejects a plan that drops a requested item, so grouping can't lose content.
- **Simulated estimates (not measurements):** a clean 6-section run takes about 250 s and about 20K tokens; with one failed attempt and one repair, about 430 s and about 31K tokens. Real runs will vary with reply length and Groq latency.
- **Rejected:** raising only the deadline and ceiling (8-section runs would still need about 7 minutes and come close to 40K); a smaller reply cap (in the simulation it didn't change the two-calls-per-minute pattern, and it increases cut-off replies); a separate cost-estimating check before execution (the plan-size limit plus re-plan achieves the same early stop with no new code).
- **Trade-off:** large documents take several minutes on the free tier, and Groq's 200K tokens per day allows only a handful of large runs a day. On a paid tier these are `.env` values.
- **Side effect:** the grouping instructions added about 140 tokens to the analyze prompt, so the analyze call now fits requests up to about **3,600 characters** (was about 3,990) within the 6,500-token per-call cap.
- **Tests:** `tests/test_plan_limits.py` (5 tests) covers the free-tier defaults, rejection of a 7-task plan, a grouped plan that keeps every item, rejection of a grouping that drops an item, and a FakeLLM pipeline run where a 7-task plan is re-planned into 6 before any section call. 10/10 tests pass.

> **Q: How did you choose 6 tasks and 480 seconds?**
> A: From the provider's rate limit, not by guessing. I simulated the real pacer offline: it allows about two section calls per minute on the free tier, so 6 sections plus a review and one repair fit in 480 s with room to spare, while 8 sections didn't fit in 300 s, which is what the live failure showed.

### Fix: a re-plan that still has too many tasks is merged in code

**What happened (live run with the 6-task limit):** the complex CRM request failed at planning: "The plan was invalid after one re-plan: The plan must have 2-6 tasks; it has 7." No sections were written.

**Checked first (offline, FakeLLM):** both prompts already receive the effective limit from settings ("a plan of 2-6 tasks", "list of 2-6 tasks"), and the re-plan receives the exact error ("The plan must have 2-6 tasks; it has 7."). A FakeLLM replay of two 7-task plans reproduced the same 502. The wiring was correct; the model just didn't follow the limit.

- **Decision:** after the re-plan, if the plan is still over `MAX_PLAN_TASKS`, code merges neighbouring tasks that use the same tool, starting from the end, until it fits. The merged task gets the title "A & B", combined covers and dependencies, and is required if either task was. Other tasks' dependencies are pointed at the merged task. The plan is then validated as usual. A warning is logged.
- **Why:** an LLM is unreliable at meeting an exact count, so the hard limit is enforced by code. The model still gets its re-plan, and code only steps in if that doesn't fit. Combining covers means no requested item can be lost, and validation still rejects any plan that leaves one uncovered.
- **Rejected:** raising the limit (the budget math sets it); a second re-plan (another LLM call with no guarantee); dropping optional sections (loses content); merging tasks with different tools (a table and a prose section can't share one tool call).
- **Limitation:** merging is by position, not meaning. Neighbouring sections are usually related (e.g. "Risks & Mitigations" and "Decisions Needed") but not always. If no neighbouring pair shares a tool, the plan still fails with a clear error.
- **Tests:** 3 new tests in `tests/test_plan_limits.py`: merging keeps every covered item; dependencies are redirected and tools are never mixed; and a regression test replaying the live failure (both plans with 7 tasks), which checks the prompts carry the limit and error and the run succeeds with 6 sections covering R1–R7. 13/13 tests pass.

> **Q: Why not just trust the re-plan prompt?**
> A: The prompt already had the exact limit and error, and the model still returned 7 tasks. Hard rules belong in code: the LLM makes the meaningful grouping when it can, and code guarantees the limit when it doesn't.

### Live rerun of the complex CRM request (after the merge fix)

**Actual result (one run, measured):** HTTP 200, **success**, 6/6 sections, **11 LLM calls (2 failed attempts), 23,941 tokens, 364.4 s**, within the 480 s deadline and well under the 40K maximum. This is consistent with the simulated estimates (about 250 s/20K clean, about 430 s/31K worst case). The response doesn't show whether the code merge was used: one call beyond analyze + 6 sections + review + 2 failed attempts was either a re-plan or a second review chunk.

**Coverage of the requested topics (checked by reading the .docx):**

| Requested | Covered? |
|---|---|
| Feasibility | Yes: "unlikely within two weeks without scope reduction" |
| Conflicting requirements | Yes, but thin: one bullet (zero budget vs 24/7 support and no downtime) |
| Labelled planning assumptions | Yes: A1–A3 in their own section and in the automatic Assumptions section |
| Phases | Yes: 6 phases over 14 days |
| Responsibilities by role | Yes: 5 roles |
| Timeline | Yes: day-by-day due dates (as bullets, not a table) |
| Risks and mitigations | Yes: 3 risks, each with a mitigation |
| Decisions needed before execution | Yes: 3 decisions |
| Changes needed to make it achievable | Weak: one recommendation (pilot a non-critical module first) |
| Vendor evaluation (stated requirement) | Yes: its own section |

**Quality issues found (not caught by the review step):**
1. **Contradictions between sections:** the vendor decision is due Day 1 in the risks section but takes Days 3–5 in the phases. The executive summary recommends a pilot of one module, but the phases plan a full production migration on Day 10.
2. **Requirements restated as lines:** "Require no downtime during migration", "Identify decisions needed before execution" and similar appear as plain, unlabelled lines. The model wrote them as record items quoting the request, so grounding accepted them.
3. **Loose assumption references:** some items cite the wrong assumption (e.g. the conflicting-requirements bullet is tagged "Assumption A2", which is about the cloud provider).

Not fixed yet; recorded so any fix is a deliberate, separate decision.

### Prompt fixes for contradictions and assumption references (fixes 1 and 3)

- **Decision:**
  - **Dependencies:** the planner sets `depends_on` when sections share dates, phases, scope or decisions (e.g. risks, decisions or the summary depend on the phases/timeline section), so the dependent section receives that section's content.
  - **Review:** the contradiction rule now names concrete cases ("different dates, order, scope or decisions for the same phase, deliverable or choice"). "Unsupported fact" now includes "an item citing an assumption that does not support it".
  - **Labels:** the content rule now says every proposal must have a visible label and that `assumption_id` is used only when that specific assumption supports the item. The `Item` schema descriptions for `label` and `assumption_id` were changed to match, because the old description ("required unless assumption_id is set") encouraged decorative assumption IDs.
- **Why:** the live CRM run showed sections written without seeing each other (the vendor decision due "Day 1" vs vendor selection on "Days 3–5") and assumption IDs used as labels.
- **Rejected:** automatically removing items that repeat request text (fix 2). It could delete legitimate constraints or recorded decisions, so those lines remain a known quality issue.
- **What is verified vs not:**
  - *Verified by code tests:* nothing new; these are prompt-only changes. The 17 existing tests and 6 smoke scripts still pass, which shows the changes broke nothing.
  - *Not verified:* whether Groq now sets these dependencies, avoids contradictions, uses only relevant assumption IDs and always adds a label. Only a live run can show that, and none has been done.
  - *Unchanged in code:* the gate still accepts a proposal that cites a known assumption without a label, and the renderer shows such items as "Assumption A1: …", so they are still visibly marked.
- **Cost (estimate):** about 76 more tokens in the analyze prompt (measured with the estimator), so the analyze call now fits requests up to about **3,450 characters** (was about 3,600). About 60 more tokens on each section and repair call (estimated from character counts). Sections that depend on the timeline also receive its content, about 600–900 tokens each.

> **Q: Why prompt changes here, when the task limit was enforced in code?**
> A: The task limit is a hard rule that code can count. Whether two sections contradict each other, or whether an assumption really supports an item, requires understanding the text, which code can't judge reliably. So the writer gets the right context and instructions, and the reviewer is told exactly what to look for. That makes these problems less likely but doesn't guarantee they're gone; a live run is the only way to check.

### Final gate: every proposal needs an explicit label

- **Decision:** `check_gate` now fails any proposal item without a label (Proposed, Recommended, Potential or Illustrative), even if it cites a valid assumption ID. An assumption ID explains a proposal's basis but no longer replaces the label. Record items are unaffected; they never need a label.
- **Why:** this aligns the code with the updated content rule and schema description, so the prompt, the reviewer ("unlabelled_proposal") and the gate all apply the same rule.
- **Trade-off:** the gate is all-or-nothing, so one unlabelled proposal that survives review and repair now fails the whole request (no document). Before, an assumption ID was enough. The reviewer's "unlabelled_proposal" check and the repair step are the chance to fix it first. Whether Groq now labels every proposal is **not verified**; no live run has been done.
- **Tests (code-verified):** in `tests/test_grounding.py`: a proposal with a valid assumption ID but no label fails; a labelled proposal passes both with and without an assumption ID; a record without a label passes. 21/21 tests pass; the 6 smoke scripts pass.

## Step 9: Tests (`tests/`, pytest + FakeLLM)

- **Decision:** the scratch smoke scripts became 82 pytest tests in 8 files, all offline. FakeLLM returns scripted replies, a fake clock makes pacing instant, and every test uses `Settings(_env_file=None)`, so results don't depend on anyone's `.env`. Shared pieces: `conftest.py` (settings, clock, client and pipeline factories), `sample_data.py` (scripted replies), `fakes.py` (the clock).
- **Why:** a reviewer can run `pytest` with no API key and see every guardrail and failure path proven in about a second. Scripts in a temp folder prove nothing to them.
- **Rejected:** live-API tests (slow, cost tokens, not repeatable); mocking deep inside the Groq SDK (FakeLLM sits at our own `Transport` boundary, so the real retry, pacing and budget code runs); installing `httpx2` to silence Starlette's TestClient warning (a new dependency for a warning; filtered in `pytest.ini` instead).

| Plan test group | Where |
|---|---|
| 1. Empty / too long / injection → 0 calls; off-topic → 1 call | `test_api.py`, `test_pipeline.py`, `test_guardrails.py` |
| 2. Invalid plan → one re-plan → failed; optional covering task forced required | `test_guardrails.py`, `test_pipeline.py`, `test_plan_limits.py` |
| 3. Required failed **or skipped** → 502 no link; optional → partial; prerequisite output in prompt | `test_pipeline.py` (includes a regression for the live deadline skip) |
| 4. Bad JSON → retry counted; truncated → failed attempt; 413 → not resent | `test_llm.py` |
| 5. Pacer waits; deadline / ceiling → call not made; over cap → not sent | `test_llm.py` |
| 6. Grounding: minutes TBC / removal; plan proposals; labels; unknown assumption | `test_grounding.py` |
| 7. Completeness: note + partial; otherwise failed; nothing substantive → failed | `test_completeness.py` |
| 8. Review: chunks within cap; oversized section → failed; repair + verify; unresolved → failed; review can't finish → failed | `test_pipeline.py` |
| 9. Gate / render failure → no link; docx reopens with headings + Assumptions; traversal rejected | `test_pipeline.py`, `test_docx.py`, `test_api.py` |

**Result:** 82 passed in about 1.1 s, no warnings.

**Known wrinkles (honest notes):**
- `AgentRequest` reads the request-length limit from the global `get_settings()` (so from `.env`), not from the app's settings, so the "too long" API test uses that value.
- One docx test calls the private `_save_verified` to force a verification failure, because it's the only way to prove a failed save leaves no file behind.
- These tests prove the code's behaviour, not Groq's: prompt quality (contradictions, labels, assumption use) can only be checked live.

> **Q: If the tests use a fake LLM, what do they actually prove?**
> A: Everything except the model's writing quality. The real retry, pacing, budget, grounding, review, gate, rendering and API code runs; only the network call is replaced. So "a required section skipped at the deadline returns 502 with no link" is proven by the code. "Groq writes consistent sections" isn't, and that's what the live runs in Step 10 check.

## Step 10: Live demos, fixes they exposed, and the README

Two required live demos were run through the real API, one call at a time. Each attempt is saved in its own `samples/attempt-N/` folder with the exact request, the JSON response and the Word file; earlier attempts are kept. Every failure was reported and approved before the next live call.

### Fixes found by the live demos

| Attempt | What went wrong | Fix | Type |
|---|---|---|---|
| Demo 1, attempt 1 (partial) | No Summary section; Attendees and Decisions each repeated all 8 records | **A.** Content rule: write only what belongs under this section's heading; prerequisite sections are for consistency, never copied. **B.** Planner: each explicitly requested section gets its own task with that item in `covers`, unless grouping is needed for the limit. **C.** Review: substantial duplication or content under the wrong heading is material; a brief restatement of key decisions in a summary is fine | Prompt |
| Demo 2, attempt 2 (502) | The planner added an unrequested "Conclusion", required by default; the repair emptied it, failing the whole run | **D.** `enforce_required`: a task is required **if and only if** it covers a requested item. **E.** Planner: if the user specifies a section count, plan exactly that many | D code (tested), E prompt |
| Demo 2, attempt 3 (incomplete) | Roles all "To be confirmed"; Assumptions section duplicated | **F.** Planning guidance: unknown team members don't stop role suggestions (job functions, never names, never implying staff are available); roles aren't `missing_info`; the content rule no longer forbids suggested owners in proposals. **G.** `Analysis.assumptions_cover`: requested items the automatic Assumptions section fulfils, counted by validation and the gate only when assumptions exist; a planned task that is only "Assumptions" is folded into the automatic section. Review: "To be confirmed" doesn't satisfy a request for suggested roles, and a missing requested item is always material | F prompt; G code (tested) + prompt |

- **Why:** each fix targets the specific failure seen, at the smallest layer that can reliably fix it: code where a rule can be checked (required flags, coverage, no duplicate section), prompts where judgement is needed (scope, roles, section count).
- **Rejected:** automatically deleting lines that repeat request text (could delete legitimate constraints or recorded decisions); weakening the gate or raising budgets to force success; a second Assumptions mechanism outside the existing automatic section.
- **Code-verified (offline):** required-from-covers (2 tests), assumption coverage in validation and the gate, schema checks on `assumptions_cover`, folding (including "Risks and Assumptions" and no-assumptions cases left untouched), and pipeline regressions for the extra "Conclusion" (now partial, not failed) and the duplicated Assumptions section. **92/92 tests pass.**
- **Live-verified only:** scope, roles, section count and review behaviour (the prompt fixes). Demo 1 attempt 2 and demo 2 attempt 4 show them working once each, which is evidence, not a guarantee.
- **Cost:** the planning call grew with each fix. Measured with the estimator, it now fits requests up to about **2,825 characters** (it was about 3,990 at Step 5), while the API still accepts 6,000. Recorded as a limitation.

### Accepted results (measured)

| Demo | Attempt | Status | Calls | Tokens | Duration |
|---|---|---|---|---|---|
| Meeting minutes | `attempt-2` | success | 6 | 9,344 | 120.7 s |
| Feedback-pilot proposal | `attempt-4` | success | 5 | 10,590 | 123.7 s |

Review notes (thin summary, empty "Value" column, an echoed request line, redundant role bullets, loose assumption citations, likely more than one page) are in the README. The one-page requirement is **not enforced** by the agent and was estimated from the word count (572 words), not measured.

> **Q: Your first demo runs failed. Isn't that a problem?**
> A: That's what the live runs were for. The offline tests prove the code's rules; the live runs showed where the model's behaviour didn't match the request. Every failure was honest (partial or 502 with a reason, never a bad document marked success), and each fix went to the smallest layer that could reliably solve it, with a regression test where code changed.

## Fix: named owners from the request were flagged as invented

A user-run live request (customer-feedback pilot, owners Abhinav, Nikita and Raghav) failed with 502. The action plan correctly used the three names from the request, but the review flagged them as material ("use job functions instead of named individuals"). Repair didn't resolve it, so the required section failed.

**Cause:** `PLANNING_GUIDANCE` contradicted itself. It called assigned owners records, then said an owner is "never a named person". Fix F (attempt 3) had added that wording to stop invented names, and the review read it as a ban on all names.

**Fix (prompt only, two lines):**
- `PLANNING_GUIDANCE`: "never invent a named person (people named in the request keep their names)".
- `REVIEW_PROMPT`: "Names the request gives (e.g. assigned owners) are records, not an issue."

- **Why:** this is a judgement rule for the model, so it belongs in the prompt. The grounding code already accepts names that appear in the request, so no code change was needed.
- **Rejected:** dropping the review's material severity for quality issues (weakens a check); removing the job-function guidance (demo 2 needs it when no owners are given).
- **Code-verified (offline):** 92/92 tests pass (no code paths changed).
- **Not yet verified:** whether the review stops flagging requested names. Only one live rerun of the same request can show this.

> **Q: How did a rule meant to prevent hallucination cause a failure?**
> A: It was worded too broadly. "Never a named person" was meant to stop invented names, but the reviewer applied it to real names from the request too. The fix says exactly what's forbidden: inventing a person. Names the user gives are records, the same as dates they give.

## Fix: planning call invented requested-item IDs for stated unknowns

The same pilot request, rerun by the user, failed at the analyze call: "missing_info refers to unknown requested items: R6, R7, R8, R9", three times. No section was written. This is unrelated to the names fix (that changed only the section and review prompts).

**Cause:** the request lists unknowns that aren't requested items (team availability, existing tools, customer data). The prompt said `missing_info` holds requested items without facts, but never said where other unknowns go, so the model invented IDs for them. The schema check rejected that correctly; the retry feedback named the bad IDs, but the model repeated the mistake.

**Fix (prompt only, one sentence in step 5 of `ANALYZE_PROMPT`):** "Each missing_info item_id must be one of your requested_items ids; unknowns that are not requested items (e.g. team availability or existing tools) belong in assumptions, not missing_info."

- **Why:** the check is right; the model needed to know where stated unknowns belong. Labelled assumptions are also what such a request asks for.
- **Rejected:** having code silently drop unknown IDs (weakens a check and hides the unknowns instead of turning them into assumptions); more retries (the same feedback had already failed twice and each retry costs a full analyze call).
- **Cost:** the planning call now fits requests up to about **2,636 characters** (was about 2,825), measured with the estimator. README updated.
- **Code-verified (offline):** 92/92 tests pass; the existing schema tests already prove unknown IDs are rejected.
- **Not yet verified:** whether the model now puts these unknowns in assumptions. Only a live rerun can show it.

> **Q: Why didn't the retry with the error message fix it?**
> A: The feedback said *what* was wrong (unknown IDs) but not *where the information should go*. The model still wanted to record the unknowns somewhere and had no rule for it. Retries fix formatting slips; a missing rule needs a prompt fix.

## Fix: review asked for an "Assumption" label the schema doesn't allow

A user-run live request (knowledge-sharing session plan) failed with 502: the review flagged the 5-day table as material because the chosen topic and duration were labelled "Proposed" instead of "clearly labelled as assumptions", as the request asked. Repair couldn't fix it, so the required section failed (11 calls, 24,099 tokens, 361 s). Both earlier fixes held: planning passed and the named owners weren't flagged.

**Cause:** an impossible review demand. "Assumption" isn't one of the four proposal labels. Assumptions become visible through the automatic Assumptions section and through an item's `assumption_id`, which renders as "Proposed / Assumption A1". The review prompt didn't know that, so it asked for a change the repair's schema can't produce.

**Fix (prompt only, two sentences):**
- `REVIEW_PROMPT`: assumptions are shown by the Assumptions section and by citing `assumption_id`; "Assumption" isn't an item label, so the fix for "label this as an assumption" is to cite its id.
- `CONTENT_RULES` (writer and repair): when the user asks for a choice to be labelled as an assumption (e.g. a chosen topic or duration), cite that assumption's id on the item.

- **Why:** the first sentence turns an unfixable issue into a fixable one (repair can set `assumption_id`); the second gets it right first time and saves a repair call. The planning call is unchanged, so its capacity (about 2,636 characters) is unchanged.
- **Rejected:** adding "Assumption" as a fifth label (duplicates the assumption-ID mechanism and changes the schema and gate); relaxing the review (weakens a check); rewording the request to avoid the failure (hides a real gap).
- **Code-verified (offline):** 92/92 tests pass (no code paths changed).
- **Not yet verified:** whether the writer cites the ids and the review accepts them. Only a live rerun can show it.

> **Q: How do you stop a self-review loop from demanding something impossible?**
> A: The reviewer must know the same output contract as the writer. Here it didn't know how assumptions are displayed, so it asked for a label that doesn't exist. I gave it the contract in one sentence, so every issue it raises is one the repair can actually fix.

## Note: free-tier daily quota and the gpt-oss-20b fallback

A user-run rerun stopped with "rate-limit wait would pass the deadline". Groq's console showed two 429s after about 104K tokens in 50 minutes; the account limits page confirms `gpt-oss-120b` allows **200K tokens per day** (also 30 requests per minute, 1K requests per day, 8K tokens per minute). The agent behaved as designed: it honoured retry-after, saw the wait wouldn't fit the deadline and stopped with no document.

- **Change:** documentation only. `.env.example` and the README now note the daily quota and that `GROQ_MODEL=openai/gpt-oss-20b` is a drop-in fallback with its own quota. No code, limits or defaults changed; the default stays 120b.
- **Why 20b:** same model family, supports JSON mode and `reasoning_effort`. Rejected: `allam-2-7b` (6K TPM is below the 6,500-token per-call cap), `qwen/qwen3.8-27b` (untested with the reasoning parameters we send).
- **Caveat:** results on 20b aren't comparable to the accepted 120b demos; record them as 20b runs.

> **Q: What happens when the provider's daily quota runs out mid-request?**
> A: Groq returns 429 with a retry-after. The client waits only if the wait plus one call's timeout fits the request deadline; otherwise the task is skipped, a required task fails the request with a clear reason, and no partial document is served. The model is a setting, so switching to another quota needs no code change.

## Fix: "To be confirmed" only where it matters, and the meeting date in minutes

A user-run minutes request on `gpt-oss-20b` produced correct facts (the missing deadline was "To be confirmed"), but every decision showed "Owner: To be confirmed; Due: To be confirmed; Value: To be confirmed", and the meeting date (a fact in the request) appeared nowhere.

**Cause:** the content rule said to fill owner, due and value only when stated, "otherwise write 'To be confirmed'", so the model applied it to fields that don't apply to a decision. The planner's standard-sections hint for minutes mentioned attendees but not the meeting date.

**Fix (prompt only, two sentences):**
- `CONTENT_RULES`: write "To be confirmed" only for a field that matters for the item (e.g. an action's owner or deadline); leave fields that don't apply (e.g. owner or value on a decision) empty.
- `ANALYZE_PROMPT`: the standard-sections example for minutes is now "meeting details such as the date and attendees".

- **Why:** both are judgement rules. Grounding is unchanged: it still turns any unsupported stated field into "To be confirmed", and leaves empty fields empty.
- **Skipped on recording day:** an optional `LLM_TEMPERATURE` setting for more consistent output. It's a code change that couldn't be checked live before recording.
- **Cost:** the planning call now fits requests up to about **2,599 characters** (was about 2,636), measured with the estimator.
- **Code-verified (offline):** 92/92 tests pass. **Not yet verified:** the effect on live output.

> **Q: Why does a decision not need "To be confirmed" for its owner?**
> A: "To be confirmed" flags a fact the reader needs and the request didn't give, like an action's deadline. A decision has no deadline or value by nature; marking those fields TBC adds noise and makes the document look broken.

## Fix: explicit judgement rules for the smaller model

A user-run complex request (document migration with a 2-week vs 3-week conflict) on `gpt-oss-20b` had the right structure and labels but wasn't presentable: it echoed an instruction ("Decide how to handle the timeline conflict") as an item, assumed IT's review "can be compressed" (assuming the conflict away), restated request facts as assumptions (the deadline, Abhinav as lead), invented "a dedicated IT sub-team", and called Abhinav "he".

**Cause:** these are judgement rules a larger model infers but our prompts never stated. Code can't check them: grounding verifies facts and labels, not whether a conflict was resolved sensibly.

**Fix (prompt only, two sentences):**
- **A.** `ANALYZE_PROMPT` step 6: never restate a fact from the request as an assumption, and never assume away a stated constraint (e.g. that a stated duration can be shortened).
- **B.** `CONTENT_RULES`: never turn the user's instructions into items, never imply people or teams are available unless the request says so, and refer to people by name or role, not gendered pronouns.

- **Also:** the demo request was reworded to state the constraint ("the review must not be skipped") and ask for "suggested owner roles". This clarifies the request; it doesn't ask for less.
- **Rejected:** code that deletes items matching request text (rejected earlier: it could delete legitimate constraints).
- **Cost:** the planning call now fits requests up to about **2,453 characters** (was about 2,599), measured with the estimator.
- **Code-verified (offline):** 92/92 tests pass. **Not yet verified:** the effect on live output.

> **Q: Your agent needed more rules on the smaller model. Is that a weakness?**
> A: It's expected. Smaller models follow explicit rules well but infer implicit ones poorly. The code guardrails (facts, labels, coverage) held on both models; only the judgement quality changed, and that's what prompts are for.

## Fix: duplicate records across sections, and clean table columns (code)

The rehearsal minutes on `gpt-oss-20b` succeeded (5 calls, 9,895 tokens, 121 s) with correct facts, the meeting date and Priya's deadline "To be confirmed", but weren't presentable: "Meeting Details" repeated both decisions and all three actions that had their own sections, and the action table's headers were the raw field names `text | owner | due | value`, with an empty value column.

**Why code this time:** the prompt already says "write only what belongs under this section's heading" and the review treats duplication as material, but the smaller model followed neither reliably. Both problems are mechanically checkable, so code fixes them every time.

**Fix:**
- `remove_duplicate_records` (grounding, run after review and before the gate): a record that appears in several sections (same type, and the same source quote or one text containing the other) is kept only in the section where items of its type make up the largest share. Ties keep both copies; proposals are never removed; a section never loses all its structured items.
- Renderer: owner/due/value columns appear only when a row uses them, and a header that just echoes the field name ("text", "owner") becomes "Item", "Owner", and so on. This removes the README limitation about empty labelled columns.

- **Rejected:** stronger prompt wording (already present and ignored by the smaller model); removing every repeated item (a brief restatement in a summary is legitimate, so ties are kept); deleting text that repeats the request (rejected earlier, a different rule).
- **Code-verified (offline):** 97/97 tests pass, including the live minutes case (a restated decision without its leading "the" still matches), ties, never emptying a section, proposals untouched, and the column headers.
- **Not yet verified live:** a rehearsal rerun of the minutes request.

> **Q: How do you decide which copy of a duplicated item to keep?**
> A: By where it fits best, measured mechanically: the section where that item type is most concentrated. A decision in a section that is all decisions belongs there, not in a mixed "details" section. On a tie I keep both copies rather than guess.

## Fix: verification review saw one section and reported the rest of the document as missing

The third minutes rehearsal on `gpt-oss-20b` failed (7 calls, 12,623 tokens, 181 s): after "Action Items" was repaired, the verification review, which is shown only the repaired section, reported that the meeting date, attendees and decisions were missing. They were in "Meeting Details" and "Decisions"; the reviewer simply couldn't see those sections.

**Why:** the first review shows all sections together, but verification re-reviews only the repaired ones, with the full request as context. With no view of the rest of the document, the model judged one section against the whole request.

**Fix (code + one prompt sentence):** every review call now lists the headings of the document's sections it isn't shown ("Other sections of the document (not shown): Meeting Details, Decisions"), and the review prompt says to judge `missing_requested_item` only against the requested items listed for the shown sections; content in another section isn't missing.

- **Rejected:** ignoring `missing_requested_item` during verification (it would weaken a check); showing every section in verification (more tokens on every repair, and the per-call cap could split them anyway).
- **Code-verified (offline):** 98/98 tests pass; a new test checks that the first review lists no "other sections" and the verification call names them. **Not yet verified live:** a rehearsal rerun.

> **Q: How did you find a bug that only appeared live?**
> A: The run card showed which step failed and why. The issue named content that was in the document, so the reviewer lacked context, not the document lacking content. The fix gives the reviewer that context instead of loosening the check.

## Fix: review invented a label rule for decisions

The first migration-plan rehearsal on `gpt-oss-20b` failed (13 calls, 27,858 tokens, 421 s, 3 failed attempts). The review flagged "Management Decisions Required" as `unlabelled_proposal` because its decision items were labelled "Proposed" "instead of the required 'proposal' label", and warned elsewhere that "decision items should not have a proposal label". Neither rule exists: "proposal" is a kind, not a label, and a decision management still has to make is exactly what a labelled proposal is for. The repair couldn't fix an issue that wasn't there, so the run failed.

**Fix (prompt):** the review prompt now names the valid labels ("Proposed", "Recommended", "Potential", "Illustrative"), says they apply to any item type, and says a decision the user still has to make carries one of them correctly.

- **Rejected:** dropping `unlabelled_proposal` issues in code when every item has a label (the issue also covers recommendations worded as agreed in paragraphs, so that would weaken a check).
- **Code-verified (offline):** 99/99 tests pass; a new test checks that the review prompt names every label in the schema, so the two can't drift apart. **Not yet verified live:** a rehearsal rerun.

> **Q: Why does the reviewer need to be told the labels when the schema already enforces them?**
> A: The schema constrains what the writer outputs, but the reviewer only sees JSON and the prompt. Without the list, the smaller model guessed a rule. Giving every LLM step the same vocabulary as the code removes the guess.

## Fix: a value field that only repeats the owner (code)

The second migration-plan rehearsal on `gpt-oss-20b` **succeeded** (12 calls, 21,532 tokens, 304 s; the review repaired T3 and T4). The content was presentable: a proposed way to handle the 2-week vs 3-week conflict, three labelled phases, two risks with mitigations, and proposed management decisions, all labelled and with no confirmed dates. One rendering problem looked like a bug: each phase line read "Owner: Abhinav; Due: To be confirmed; Value: Owner: Abhinav (Project Lead)", with the owner repeated in `value`.

**Fix (renderer):** a `value` that contains the item's owner is not shown, in item lines or in tables (and a column with nothing left to show is hidden as before).

- **Rejected:** a prompt rule about the `value` field (a smaller model may ignore it, and this is mechanically checkable); clearing `value` in grounding (the data is correct, it's only redundant on screen).
- **Code-verified (offline):** 100/100 tests pass, including the live case as an item line and as a table row.
- **Seen, not fixed:** the run produced no assumptions, so the plan says "No planning assumptions were needed" and doesn't mention the unknowns the request names (document count, storage tool, IT availability).

> **Q: Why fix this in the renderer, not the model?**
> A: The model's data was correct, just redundant. Presentation rules belong in the presentation layer, where they apply every time without spending tokens.

## Request change: ask for assumptions in the complex demo

The successful migration-plan run had an empty Assumptions section ("No planning assumptions were needed") although the request names three unknowns. The complex demo is meant to show the agent making reasonable assumptions, so the request now adds: "State your assumptions about the unknowns."

- **Why the request, not the prompt:** the analyze prompt already says unknowns belong in assumptions; the smaller model skipped it. As an explicit request, it becomes a requested item, and the completeness gate checks that assumptions cover it.
- **Cost:** the request is about 700 characters, well under the planning capacity of about 2,453.
- **Not yet verified live.**

**Update (time limit):** the first run with the assumptions sentence ran out of time (7 calls, 14,965 tokens, 447 s; a repair of the phases section couldn't start before the 480 s deadline). Free-tier waits made each call slow. The deadline was not raised. Instead, the request drops "list the decisions management must confirm": one section fewer means fewer calls and less time, and that section caused the earlier label confusion. The request still has a conflict (2 vs 3 weeks), missing information (three unknowns), and asks the agent to choose the approach and state its assumptions.

**Update (overlapping sections):** the 3-section request succeeded (8 calls, 18,115 tokens, 302 s) and the Assumptions section now named the unknowns, but it wasn't presentable. "Timeline Conflict Resolution" contained its own three phases and two risks, which duplicated and contradicted the later sections (different phase names and owners), and it echoed the instruction "Suggest a realistic way to handle the timeline conflict" as an item. The writer already gets the document outline and the rule "write only what belongs under this section's heading"; the smaller model ignored both. These were proposals, so the duplicate-record cleanup (records only) correctly didn't touch them. The request now asks for "three phases ... that handle the timeline conflict realistically", so there is no separate conflict section to overlap with the phases.
- **Rejected:** a duplicate cleanup for proposals (proposals are reworded freely, so matching them is unreliable and could delete legitimate content); a stronger prompt rule (the existing one was ignored).

**Update (plan with one task):** the merged request failed at planning: "The plan must have 2-6 tasks; it has 1", even after one re-plan. The likely cause (no server log was available): the model planned a content task plus an "Assumptions" task, and code folds the latter into the automatic Assumptions section, leaving one task. The re-plan was never told that folding happens. **Fix (code):** a plan with too few tasks now also gets the error "Split the content into more sections; an Assumptions-only section does not count, because it is added automatically." The minimum of 2 tasks was not lowered. 101/101 tests pass; not verified live.

**Update (recording):** with about one migration run of quota left, the recording uses the original request (rehearsal 2 succeeded with it). samples/04 is back to that text.
