"""Four-agent support demo built on one generic tool-calling loop.

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
    -> transfer_to_billing_specialist    (chained handoff, if it turns out
                                           to actually be a billing issue)

Every model call and every tool call (including handoffs, which are just
tools with is_handoff=True) is recorded through the agento11y client, so a
single /chat request produces a trace with one generation per agent turn
and one tool-execution span per tool or handoff:

  http.chat_request
    -> generation (concierge)
       -> tool_execution (transfer_to_account_specialist)      [on handoff]
          -> generation (account-specialist)
             -> tool_execution (lookup_account)                [if invoked]
             -> generation (account-specialist, follow-up)
                -> tool_execution (transfer_to_billing_specialist)  [chained]
                   -> generation (billing-specialist)
                      -> tool_execution (lookup_invoice)        [if invoked]
                      -> generation (billing-specialist, follow-up)

Claude can request more than one tool in a single turn (e.g. "check the
account and reset the password" -> lookup_account + reset_password
together). The agent loop below handles that: every tool_use block in a
turn gets its own tool_execution span and its own tool_result, all in one
follow-up message -- the Anthropic API 400s if any tool_use block doesn't
get a matching tool_result in the very next message. A handoff tool is the
one exception: it jumps to a fresh conversation with the target agent and
never sends this one back, so if it's requested alongside other tools in
the same turn, the others are simply moot.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import anthropic
from agento11y import Client, ToolExecutionStart, with_agent_name, with_agent_version, with_conversation_id
from agento11y_anthropic import AnthropicOptions, messages
from dotenv import load_dotenv

from .tools import (
    CHECK_SYSTEM_STATUS_TOOL,
    ISSUE_REFUND_TOOL,
    LOOKUP_ACCOUNT_TOOL,
    LOOKUP_INVOICE_TOOL,
    RESET_PASSWORD_TOOL,
    TRANSFER_TO_ACCOUNT_TOOL,
    TRANSFER_TO_BILLING_TOOL,
    TRANSFER_TO_TECHNICAL_TOOL,
    check_system_status,
    issue_refund,
    lookup_account,
    lookup_invoice,
    reset_password,
)

# Load .env when running locally (uvicorn, pytest, etc). Docker Compose
# injects AGENTO11Y_*/ANTHROPIC_*/OTEL_* vars directly via `env_file:`, so
# this is a no-op in that case, but it's required for a bare `uvicorn` run.
load_dotenv()

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
AGENT_VERSION = "1.0.0"
MAX_TURNS = 4  # per-agent cap on tool-call round trips, to bound a runaway loop

_anthropic_client = anthropic.Anthropic()


# --- Agent definitions ---------------------------------------------------


@dataclass
class ToolHandler:
    fn: Optional[Callable[..., dict]] = None
    is_handoff: bool = False
    target: Optional["AgentDef"] = None


@dataclass
class AgentDef:
    name: str
    system_prompt: str
    tools: list = field(default_factory=list)
    tool_handlers: dict = field(default_factory=dict)


BILLING_SYSTEM = (
    "You are Acme Cloud's billing specialist. Help with invoice, payment, and "
    "refund questions. If given or you can infer a specific invoice ID "
    "(format INV-####), call lookup_invoice before answering. If the user "
    "wants a refund, call issue_refund (it will tell you if the invoice isn't "
    "eligible). Keep answers short."
)

TECHNICAL_SYSTEM = (
    "You are Acme Cloud's technical support specialist. Help diagnose service "
    "issues. If the user names a service (api, database, auth, or "
    "billing-service) or asks whether something is down, call "
    "check_system_status. Keep answers short and practical."
)

ACCOUNT_SYSTEM = (
    "You are Acme Cloud's account and security specialist. Help with account "
    "lookups, plan questions, and password resets. If given or you can infer "
    "an account ID (format ACC-##), call lookup_account before answering. If "
    "asked to reset a password, call reset_password. If the question turns "
    "out to actually be about an invoice, payment, or refund, call "
    "transfer_to_billing_specialist instead of handling it yourself."
)

CONCIERGE_SYSTEM = (
    "You are the front-line support concierge for Acme Cloud. Answer general "
    "product questions directly, in two or three sentences. Otherwise route to "
    "a specialist: call transfer_to_billing_specialist for invoices, payments, "
    "or refunds; transfer_to_technical_specialist for outages, errors, or "
    "service status; or transfer_to_account_specialist for account, plan, or "
    "password questions."
)

billing_agent_def = AgentDef(
    name="billing-specialist",
    system_prompt=BILLING_SYSTEM,
    tools=[LOOKUP_INVOICE_TOOL, ISSUE_REFUND_TOOL],
    tool_handlers={
        "lookup_invoice": ToolHandler(fn=lookup_invoice),
        "issue_refund": ToolHandler(fn=issue_refund),
    },
)

technical_agent_def = AgentDef(
    name="technical-specialist",
    system_prompt=TECHNICAL_SYSTEM,
    tools=[CHECK_SYSTEM_STATUS_TOOL],
    tool_handlers={
        "check_system_status": ToolHandler(fn=check_system_status),
    },
)

account_agent_def = AgentDef(
    name="account-specialist",
    system_prompt=ACCOUNT_SYSTEM,
    tools=[LOOKUP_ACCOUNT_TOOL, RESET_PASSWORD_TOOL, TRANSFER_TO_BILLING_TOOL],
    tool_handlers={
        "lookup_account": ToolHandler(fn=lookup_account),
        "reset_password": ToolHandler(fn=reset_password),
        "transfer_to_billing_specialist": ToolHandler(is_handoff=True, target=billing_agent_def),
    },
)

concierge_agent_def = AgentDef(
    name="concierge",
    system_prompt=CONCIERGE_SYSTEM,
    tools=[TRANSFER_TO_BILLING_TOOL, TRANSFER_TO_TECHNICAL_TOOL, TRANSFER_TO_ACCOUNT_TOOL],
    tool_handlers={
        "transfer_to_billing_specialist": ToolHandler(is_handoff=True, target=billing_agent_def),
        "transfer_to_technical_specialist": ToolHandler(is_handoff=True, target=technical_agent_def),
        "transfer_to_account_specialist": ToolHandler(is_handoff=True, target=account_agent_def),
    },
)


# --- Generic agent loop ---------------------------------------------------


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts).strip() or "(no text response)"


def _all_tool_uses(response):
    return [block for block in response.content if block.type == "tool_use"]


def _provider_call(req):
    return _anthropic_client.messages.create(**req)


def _run_agent(
    client: Client,
    conversation_id: str,
    agent_def: AgentDef,
    user_message: str,
    handoff_reason: Optional[str] = None,
) -> dict:
    first_content = (
        f"{user_message}\n\n(Handoff reason: {handoff_reason})" if handoff_reason else user_message
    )
    conversation = [{"role": "user", "content": first_content}]

    base_request = {
        "model": MODEL,
        "max_tokens": 500,
        "system": agent_def.system_prompt,
        "tools": agent_def.tools,
    }
    options = AnthropicOptions(
        conversation_id=conversation_id,
        agent_name=agent_def.name,
        agent_version=AGENT_VERSION,
    )

    with with_agent_name(agent_def.name), with_agent_version(AGENT_VERSION):
        for _ in range(MAX_TURNS):
            response = messages.create(
                client, {**base_request, "messages": conversation}, _provider_call, options
            )
            tool_uses = _all_tool_uses(response)

            if not tool_uses:
                return {"agent": agent_def.name, "reply": _extract_text(response)}

            # Claude can request several tools in one turn. The API requires
            # a tool_result for every tool_use block in the *next* message, or
            # the following call 400s -- so a handoff needs special handling
            # (it jumps to a fresh conversation and never sends this one back,
            # so any other tool_use blocks alongside it are simply moot), and
            # otherwise every requested tool gets executed and answered.
            handoff_use = next(
                (
                    tu
                    for tu in tool_uses
                    if (h := agent_def.tool_handlers.get(tu.name)) and h.is_handoff
                ),
                None,
            )

            if handoff_use is not None:
                handler = agent_def.tool_handlers[handoff_use.name]
                with client.start_tool_execution(
                    ToolExecutionStart(
                        tool_name=handoff_use.name,
                        tool_call_id=handoff_use.id,
                        tool_type="function",
                        include_content=True,
                    )
                ) as rec:
                    result = _run_agent(
                        client,
                        conversation_id,
                        handler.target,
                        user_message,
                        handoff_reason=handoff_use.input.get("reason"),
                    )
                    rec.set_result(arguments=handoff_use.input, result=result)
                return result

            conversation.append({"role": "assistant", "content": response.content})

            tool_result_blocks = []
            for tool_use in tool_uses:
                handler = agent_def.tool_handlers.get(tool_use.name)

                if handler is None:
                    # The model called a tool that doesn't exist for this
                    # agent. Still owes a tool_result, or the next call 400s.
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"error": f"unknown tool '{tool_use.name}'"}),
                            "is_error": True,
                        }
                    )
                    continue

                with client.start_tool_execution(
                    ToolExecutionStart(
                        tool_name=tool_use.name,
                        tool_call_id=tool_use.id,
                        tool_type="function",
                        include_content=True,
                    )
                ) as rec:
                    tool_result = handler.fn(**tool_use.input)
                    rec.set_result(arguments=tool_use.input, result=tool_result)

                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(tool_result),
                    }
                )

            conversation.append({"role": "user", "content": tool_result_blocks})

    return {
        "agent": agent_def.name,
        "reply": "Sorry, I wasn't able to resolve this in the time I had -- could you rephrase?",
    }


def handle_message(client: Client, conversation_id: str, user_message: str) -> dict:
    with with_conversation_id(conversation_id):
        return _run_agent(client, conversation_id, concierge_agent_def, user_message)
