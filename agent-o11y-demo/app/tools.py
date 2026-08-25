"""Mock tools the agents can call. No external services involved -- this
is deliberately fake data so the demo runs with nothing but an Anthropic
API key and Grafana Cloud credentials."""

# --- Mock data stores -------------------------------------------------

_INVOICES = {
    "INV-1001": {"status": "paid", "amount_usd": 129.00, "due_date": "2026-07-01"},
    "INV-1002": {"status": "overdue", "amount_usd": 49.99, "due_date": "2026-06-15"},
    "INV-1003": {"status": "pending", "amount_usd": 899.00, "due_date": "2026-08-30"},
}

_SERVICES = {
    "api": {"status": "operational", "uptime_30d": "99.98%"},
    "database": {"status": "degraded", "uptime_30d": "97.20%"},
    "auth": {"status": "operational", "uptime_30d": "99.99%"},
    "billing-service": {"status": "operational", "uptime_30d": "99.95%"},
}

_ACCOUNTS = {
    "ACC-01": {"plan": "pro", "status": "active", "email": "a***@example.com"},
    "ACC-02": {"plan": "free", "status": "suspended", "email": "b***@example.com"},
}


# --- Tool implementations ----------------------------------------------


def lookup_invoice(invoice_id: str) -> dict:
    return _INVOICES.get(invoice_id, {"status": "not_found", "invoice_id": invoice_id})


def issue_refund(invoice_id: str) -> dict:
    invoice = _INVOICES.get(invoice_id)
    if invoice is None:
        return {"status": "not_found", "invoice_id": invoice_id}
    if invoice["status"] not in ("paid", "overdue"):
        return {
            "status": "not_eligible",
            "invoice_id": invoice_id,
            "current_status": invoice["status"],
        }
    invoice["status"] = "refunded"
    return {"status": "refunded", "invoice_id": invoice_id, "amount_usd": invoice["amount_usd"]}


def check_system_status(service_name: str) -> dict:
    return _SERVICES.get(
        service_name.lower(), {"status": "unknown_service", "service_name": service_name}
    )


def lookup_account(account_id: str) -> dict:
    return _ACCOUNTS.get(account_id, {"status": "not_found", "account_id": account_id})


def reset_password(account_id: str) -> dict:
    if account_id not in _ACCOUNTS:
        return {"status": "not_found", "account_id": account_id}
    return {"status": "reset_link_sent", "account_id": account_id}


# --- Tool schemas (Anthropic tool-use format) --------------------------

LOOKUP_INVOICE_TOOL = {
    "name": "lookup_invoice",
    "description": "Look up an invoice by ID and return its status, amount, and due date.",
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_id": {
                "type": "string",
                "description": "Invoice ID, formatted like INV-1001.",
            }
        },
        "required": ["invoice_id"],
    },
}

ISSUE_REFUND_TOOL = {
    "name": "issue_refund",
    "description": (
        "Issue a refund for an invoice. Only works if the invoice is 'paid' or "
        "'overdue'; check its status with lookup_invoice first if unsure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_id": {
                "type": "string",
                "description": "Invoice ID, formatted like INV-1001.",
            }
        },
        "required": ["invoice_id"],
    },
}

CHECK_SYSTEM_STATUS_TOOL = {
    "name": "check_system_status",
    "description": (
        "Check the operational status and 30-day uptime of a service. "
        "Known services: api, database, auth, billing-service."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": "Service name, e.g. 'api', 'database', 'auth', 'billing-service'.",
            }
        },
        "required": ["service_name"],
    },
}

LOOKUP_ACCOUNT_TOOL = {
    "name": "lookup_account",
    "description": "Look up an account by ID and return its plan and status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Account ID, formatted like ACC-01.",
            }
        },
        "required": ["account_id"],
    },
}

RESET_PASSWORD_TOOL = {
    "name": "reset_password",
    "description": "Send a password reset link for an account.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Account ID, formatted like ACC-01.",
            }
        },
        "required": ["account_id"],
    },
}


def _handoff_tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short reason for the handoff, for the specialist's context.",
                }
            },
            "required": ["reason"],
        },
    }


TRANSFER_TO_BILLING_TOOL = _handoff_tool(
    "transfer_to_billing_specialist",
    "Hand off to the billing specialist for invoice, payment, refund, or billing account questions.",
)

TRANSFER_TO_TECHNICAL_TOOL = _handoff_tool(
    "transfer_to_technical_specialist",
    "Hand off to the technical specialist for outages, errors, or service status questions.",
)

TRANSFER_TO_ACCOUNT_TOOL = _handoff_tool(
    "transfer_to_account_specialist",
    "Hand off to the account specialist for account lookups, plan questions, or password resets.",
)
