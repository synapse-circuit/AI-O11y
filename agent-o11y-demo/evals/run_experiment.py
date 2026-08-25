#!/usr/bin/env python3
"""STARTER RUNNER -- review before you rely on it.

Runs the four agents in app/agents.py over evals/acme-support-starter.yaml as
an Agent Observability *experiment* and publishes per-case scores to Grafana
Cloud. This is the offline/dev-time half of evaluation: run it before and
after a prompt or tool change and compare.

How this tracks quality *across versions*: bump AGENT_VERSION in
app/agents.py whenever you change a prompt or a tool (billing, technical,
account, or concierge -- they currently share one version string), then run
this file again. Each run's scores are tagged with that agent_version, so
Grafana Cloud's Quality / Experiments view can show one version's scores next
to another's instead of one blurred-together number.

This script does NOT create, enable, or modify any evaluator, rule, or guard
in your Grafana Cloud tenant. Those are a separate, Cloud-side concept for
scoring *live* production traffic, configured in the AI Observability plugin
once you have real conversations flowing. Nothing here touches that -- this
only publishes the results of test cases you (or an LLM judge) already
decided how to grade, as one offline experiment run.

You should still:
  1. Read through evals/acme-support-starter.yaml -- the edge/adversarial
     cases encode one reasonable judgment call about correct behavior each;
     adjust any you disagree with, and add real cases from actual usage once
     you have some.
  2. Tune judge_task_completion()'s prompt/threshold once you see real
     failures -- it's a starting point, not a validated rubric.
  3. Add the other evaluators noted in judge_task_completion()'s docstring
     the same way, if you want separate scores for them instead of folding
     them into one judge.

Requires the same env vars as running the app itself (from .env):
    ANTHROPIC_API_KEY
    AGENTO11Y_ENDPOINT, AGENTO11Y_PROTOCOL, AGENTO11Y_AUTH_MODE,
    AGENTO11Y_AUTH_TENANT_ID, AGENTO11Y_AUTH_TOKEN

Optional:
    AGENTO11Y_INGEST_ACTOR  -- keep this stable across runs. A run and its
        trials must share one actor, or trial creation fails with
        "401: experiment is owned by another actor". Defaults to
        "ingest:sdk/python".
    AGENTO11Y_EXPERIMENT_ID -- defaults to a fresh timestamp-based id per run.
        Reusing an id created by a different actor also 401s.

Run it:
    python evals/run_experiment.py
"""

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import anthropic  # noqa: E402
from agento11y import experiments  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.agents import AGENT_VERSION, MODEL, handle_message  # noqa: E402
from app.telemetry import get_agento11y_client  # noqa: E402

SUITE_PATH = Path(__file__).parent / "acme-support-starter.yaml"
PASS_THRESHOLD = 0.6  # per-trial cutoff, and the overall pass-rate gate below

_grader_client = anthropic.Anthropic()


def run_agent(client, case) -> dict:
    """Call the real agent system for one eval case. Returns {"agent", "reply"}."""
    prompt = case.input["prompt"] if isinstance(case.input, dict) else str(case.input)
    conversation_id = f"eval-{case.test_case_id}-{int(time.time())}"
    return handle_message(client, conversation_id, prompt)


def score_routing(case, result):
    """Deterministic: did the right agent end up answering?

    Returns (passed, detail) or None if this case doesn't specify an expected
    agent (not every case needs a routing check).
    """
    expected = case.expected.get("agent") if isinstance(case.expected, dict) else None
    if expected is None:
        return None
    acceptable = expected if isinstance(expected, list) else [expected]
    passed = result["agent"] in acceptable
    return passed, f"expected agent in {acceptable}, got '{result['agent']}'"


def judge_task_completion(case, result):
    """Sketched llm_judge -- one model call grading whether the reply reflects
    the ground-truth facts for this case. This is the one evaluator wired up
    end to end; tune its prompt and PASS_THRESHOLD once you see real failures.

    To add the other evaluators worth having here, the same way:
      - tool_call_correct: grade whether the reply's specifics (amounts,
        statuses) match app/tools.py's mock data -- i.e. the agent actually
        called the tool instead of guessing a plausible-sounding answer.
      - format_adherence (deterministic, cheap): check word count against the
        "keep answers short" instruction in every system prompt, and check no
        raw JSON / tool-result text leaked into the user-facing reply.
      - edge_case_handling: currently folded into this judge's rubric for the
        `edge` / `adversarial` categories; split it into its own judge if you
        want a separate score to track those specifically.

    Once you have live traffic, these same criteria can also run *online* --
    as Agent Observability rules over ingested conversations, or as SDK guard
    hooks on the request path. Those are separate, Cloud-configured surfaces;
    not set up by this script.
    """
    facts = case.expected.get("facts", "") if isinstance(case.expected, dict) else str(case.expected)
    prompt_text = case.input["prompt"] if isinstance(case.input, dict) else str(case.input)

    grading_prompt = (
        "You are grading a customer support reply. Return strict JSON only, no "
        'other text: {"score": 0-1, "passed": true or false, "explanation": '
        '"one sentence"}.\n\n'
        f"USER REQUEST:\n{prompt_text}\n\n"
        f"GROUND TRUTH THE REPLY SHOULD REFLECT:\n{facts}\n\n"
        f"ACTUAL REPLY (from agent '{result['agent']}'):\n{result['reply']}\n\n"
        "Score 1.0 if the reply is accurate, on-topic, and consistent with the "
        "ground truth. Score 0.0 if it contradicts the ground truth, invents "
        "facts, or ignores the request. Use values in between for partial credit."
    )

    response = _grader_client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": grading_prompt}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start : end + 1]) if 0 <= start <= end else {}

    score = max(0.0, min(1.0, float(data.get("score", 0.0))))
    passed = bool(data.get("passed", score >= PASS_THRESHOLD))
    explanation = str(data.get("explanation", "(judge returned no explanation)"))
    return score, passed, explanation


def run_suite(experiment_id: str | None = None) -> dict:
    """Run the full eval suite once and return a summary dict. This is the
    reusable core: the CLI entrypoint below calls it, and so does the
    POST /evals/run endpoint in app/main.py (the "Run evaluations" button
    in the chat UI). Each call creates its own agento11y client and its own
    Grafana Cloud experiment run -- safe to call repeatedly, including
    concurrently with the app's normal chat traffic.
    """
    suite = experiments.TestSuite.from_yaml(str(SUITE_PATH))
    verifier = experiments.Evaluator(
        evaluator_id="routing_and_task_completion", version="starter-0", kind="llm_judge"
    )

    client = get_agento11y_client()

    candidate = {
        "agent_name": "acme-support",
        "agent_version": AGENT_VERSION,
        "model_name": MODEL,
    }

    experiment_id = experiment_id or os.getenv(
        "AGENTO11Y_EXPERIMENT_ID", f"acme-support-starter-{int(time.time())}"
    )

    results = []
    experiment_url = None

    try:
        with experiments.experiment(
            name="acme-support starter",
            experiment_id=experiment_id,
            suite=suite,
            planned_trial_count=len(suite.test_cases),
            candidate=candidate,
            tags=["starter", AGENT_VERSION],
            actor=os.getenv("AGENTO11Y_INGEST_ACTOR", "ingest:sdk/python"),
        ) as exp:
            for case in suite.test_cases:
                with exp.trial(case) as trial:
                    result = run_agent(client, case)
                    prompt_text = (
                        case.input["prompt"] if isinstance(case.input, dict) else str(case.input)
                    )

                    trial.record_io(
                        input=prompt_text,
                        output=result["reply"],
                        model_provider="anthropic",
                        model_name=MODEL,
                    )

                    routing = score_routing(case, result)
                    task_score, task_passed, task_why = judge_task_completion(case, result)

                    if routing is not None and not routing[0]:
                        # Wrong agent handled it -- that overrides the content
                        # grade, since a correct-sounding answer from the
                        # wrong specialist is still a routing failure.
                        final_score, final_passed = 0.0, False
                        explanation = f"ROUTING FAILED ({routing[1]}); content grade was {task_score:.2f}"
                    else:
                        final_score, final_passed = task_score, task_passed
                        explanation = task_why

                    trial.final_score(
                        final_score, passed=final_passed, explanation=explanation, evaluator=verifier
                    )

                    results.append(
                        {
                            "id": case.test_case_id,
                            "prompt": prompt_text,
                            "agent": result["agent"],
                            "reply": result["reply"],
                            "score": final_score,
                            "passed": final_passed,
                            "explanation": explanation,
                        }
                    )
            experiment_url = exp.url
    finally:
        client.shutdown()

    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)
    pass_rate = passed_count / total if total else 0.0

    return {
        "experiment_id": experiment_id,
        "experiment_url": experiment_url,
        "agent_version": AGENT_VERSION,
        "results": results,
        "passed_count": passed_count,
        "total": total,
        "pass_rate": pass_rate,
    }


def main() -> None:
    summary = run_suite()

    for r in summary["results"]:
        print(
            f"  {r['id']:38s} agent={r['agent']:20s} "
            f"score={r['score']:.2f} passed={r['passed']}"
        )

    print(f"\n{summary['passed_count']}/{summary['total']} passed ({summary['pass_rate']:.0%})")
    print(f"Experiment: {summary['experiment_url']}")

    if summary["pass_rate"] < PASS_THRESHOLD:
        print(f"Pass rate below threshold ({PASS_THRESHOLD:.0%}) -- failing.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
