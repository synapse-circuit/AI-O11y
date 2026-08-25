"""FastAPI wrapper around the two-agent demo. This is the process boundary
that Grafana Agent Observability traces should be read alongside: each
/chat call opens one HTTP-level span, and everything the agents do nests
underneath it.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from evals.run_experiment import run_suite

from .agents import handle_message
from .telemetry import configure_metrics, configure_tracing, get_agento11y_client

tracer = configure_tracing()
configure_metrics()
agento11y_client = get_agento11y_client()

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    agento11y_client.shutdown()


app = FastAPI(title="AI O11y Agent Demo", lifespan=lifespan)


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    agent: str
    reply: str


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    with tracer.start_as_current_span("http.chat_request") as span:
        span.set_attribute("conversation.id", req.conversation_id)
        result = handle_message(agento11y_client, req.conversation_id, req.message)
        span.set_attribute("agent.selected", result["agent"])
        return result


@app.post("/evals/run")
def run_evals():
    """Runs evals/acme-support-starter.yaml end to end and publishes the
    results as a Grafana Cloud Agent Observability experiment. Real Claude
    calls, real Grafana Cloud writes -- this is the "Run evaluations" button
    in the chat UI, not a mock. Takes roughly a minute for the 10-case suite.
    """
    with tracer.start_as_current_span("http.evals_run"):
        try:
            return run_suite()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
