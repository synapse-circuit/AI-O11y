# AI O11y Agent Demo

A small multi-agent support app that demonstrates Grafana Agent
Observability end to end. A concierge agent answers general questions
directly or routes to one of three specialists, each with its own tools.
One specialist can chain a handoff to another. Every model call and tool
call is recorded, so a single request produces a full trace.

```
concierge
  -> transfer_to_billing_specialist    (handoff)
  -> transfer_to_technical_specialist  (handoff)
  -> transfer_to_account_specialist    (handoff)

billing-specialist
  -> lookup_invoice, issue_refund      (tools)

technical-specialist
  -> check_system_status               (tool)

account-specialist
  -> lookup_account, reset_password    (tools)
  -> transfer_to_billing_specialist    (chained handoff)
```

A chained-handoff example: ask the account specialist about a refund and
it hands off to billing mid-conversation, giving a trace like:

```
http.chat_request
  -> generation (concierge)
     -> tool_execution (transfer_to_account_specialist)
        -> generation (account-specialist)
           -> tool_execution (lookup_account)
           -> generation (account-specialist, follow-up)
              -> tool_execution (transfer_to_billing_specialist)
                 -> generation (billing-specialist)
                    -> tool_execution (lookup_invoice)
                    -> generation (billing-specialist, follow-up)
```

## Stack

- Python 3.12, FastAPI
- Anthropic Claude for all four agents
- [`agento11y`](https://github.com/grafana/agento11y) (Grafana's Agent Observability SDK) for generation and tool-call telemetry
- OpenTelemetry SDK for traces and metrics, exported over OTLP to Grafana Cloud
- A single static HTML page (`app/static/index.html`) as a chat UI, served by FastAPI -- no build step, no frontend framework
- `agento11y`'s `experiments` module for offline evaluation runs (`evals/`), scored and published as part of the same SDK

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `ANTHROPIC_API_KEY` — your Anthropic API key.
   - The `AGENTO11Y_*` values — from the **Connection** tab of the AI
     Observability plugin in your Grafana Cloud stack
     (`https://<your-stack>.grafana.net/plugins/grafana-sigil-app`). Create an
     access policy token scoped with `sigil:write`, `metrics:write`,
     `traces:write`, and `logs:write`; it covers both `AGENTO11Y_AUTH_TOKEN`
     and the OTLP headers below.
   - The `OTEL_EXPORTER_OTLP_*` values — same page, the OTLP gateway URL for
     your stack's region, with `Authorization: Basic <base64(instanceID:apiToken)>`
     as the header.

2. Run it.

   With Docker:

   ```bash
   docker compose up --build
   ```

   Without Docker:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload
   ```

3. Talk to it.

   Open `http://localhost:8000/` for the chat UI — it shows which agent
   answered each message, and a "New conversation" button starts a fresh
   `conversation_id` so traces don't bleed together.

   Or use curl against the API directly:

   ```bash
   curl -X POST localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"conversation_id": "demo-1", "message": "What is your refund policy?"}'

   curl -X POST localhost:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"conversation_id": "demo-2", "message": "Can you check the status of invoice INV-1002?"}'
   ```

   Things to try, in the UI or via curl:

   - "What's your refund policy?" — concierge answers directly.
   - "Can you check invoice INV-1002?" — hands off to billing, which looks
     up the invoice.
   - "Is the database down?" — hands off to technical, which checks status.
   - "I need to reset the password for ACC-01" — hands off to account.
   - "I'm on ACC-02, can I get a refund for INV-1003?" — hands off to
     account, which then chains a handoff to billing.

## What to look for in Grafana Cloud

Open the AI Observability plugin in your stack after sending a few requests:

- The **conversation** view shows each request, linked to its generations.
- Each generation shows `agent_name` (`concierge`, `billing-specialist`,
  `technical-specialist`, or `account-specialist`), the model used, token
  counts, and latency.
- Tool executions (including handoffs) appear nested under their parent
  generation — the chained account-to-billing example shows two levels of
  nesting.
- The Explore/Tempo view shows the same structure as an OTel trace, rooted
  at the `http.chat_request` span from FastAPI.

## Evaluations: tracking quality across versions

`evals/` holds an offline evaluation suite you run from your machine (or CI)
before and after a prompt or tool change, to catch regressions instead of
noticing them in production:

- `evals/acme-support-starter.yaml` — 10 test cases (3 happy, 4 edge, 3
  adversarial) covering all four agents: general questions, each specialist's
  tools, the account-to-billing handoff path, unknown invoice/service IDs,
  and three adversarial prompts (system-prompt leak, persona override,
  fabricated refund amount).
- `evals/run_experiment.py` — runs the suite through the real agents
  (`handle_message()`, unmodified) and publishes scores to Grafana Cloud as
  an Agent Observability *experiment*. Two things get checked per case: a
  deterministic `routing` check (did the right agent answer?) and an
  `llm_judge` `task_completion` check (does the reply match the ground truth
  in the YAML's `facts` field?). A routing failure overrides the content
  score, since a plausible-sounding answer from the wrong specialist is
  still wrong.

**From the chat UI:** click "Run evaluations" (top right of `http://localhost:8000/`).
It calls the same `run_suite()` the CLI uses, shows a confirmation dialog first
(it makes ~20 real Claude calls and writes to Grafana Cloud), then renders a
table of per-case results plus a link to the run in Grafana Cloud. Grafana
Cloud's own AI Observability UI has no equivalent "run" button of its own --
it only displays results after an SDK-driven run publishes them, which is
what this button (or the CLI below) does.

**From the command line:**

```bash
python evals/run_experiment.py
```

Or through Docker, reusing the same image and `.env` as the running app:

```bash
docker compose build
docker compose run --rm agent-demo python evals/run_experiment.py
```

It prints a pass/fail line per case, an overall pass rate, and a link to the
run in Grafana Cloud. It exits non-zero if the pass rate drops below 60%, so
you can wire it into CI as a gate on pull requests that touch `app/agents.py`
or `app/tools.py`.

**How this tracks quality across versions:** every agent shares one
`AGENT_VERSION` string in `app/agents.py` (currently `"1.0.0"`). Bump it when
you change a prompt or a tool, then run the eval suite again — Grafana
Cloud's Quality/Experiments view groups scores by `agent_version`, so you can
see the new version's pass rate next to the old one instead of one number
that silently drifts.

Two things this setup deliberately does *not* do:

- **It doesn't create anything in your Grafana Cloud tenant.** No
  evaluator, rule, or guard gets created or modified — this only publishes
  the results of one experiment run, using evaluators defined as plain
  Python in `run_experiment.py`.
- **It's offline, not live.** Once you have real production traffic, the
  same evaluation criteria can also run *online* — as Agent Observability
  rules over ingested conversations, or as SDK guard hooks on the request
  path — but those are separate, Cloud-configured surfaces (set up in the AI
  Observability plugin itself), not something this repo's code sets up.

Before trusting it, review the YAML's edge/adversarial cases — they encode
one reasonable judgment call each about correct behavior, not a validated
spec — and tune `judge_task_completion()`'s prompt once you see it grade a
few real failures. Its docstring in `run_experiment.py` also notes three
more evaluators worth adding the same way (`tool_call_correct`,
`format_adherence`, a split-out `edge_case_handling`) once this one is
proven out.

## Notes

- All tool data (`app/tools.py`) is hardcoded mock data — three invoices,
  four services, two accounts. There's no real backend behind any of it,
  on purpose, so the demo has no dependencies beyond Anthropic and Grafana
  Cloud.
- Each agent runs its own bounded tool-calling loop (`MAX_TURNS = 4` in
  `app/agents.py`) so a model that keeps calling tools can't loop forever.
- If you leave the `OTEL_EXPORTER_OTLP_*` variables unset, the app still
  runs and generation/tool telemetry still ships via `agento11y` (as long as
  the `AGENTO11Y_*` vars are set) — you just won't get OTel traces/metrics.
- Swap `ANTHROPIC_MODEL` to a stronger model (e.g. `claude-sonnet-5`) if you
  want better routing judgment; the default is chosen for low cost.
# AI-O11y
