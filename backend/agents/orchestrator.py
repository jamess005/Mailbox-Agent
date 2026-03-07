"""
orchestrator.py — LangGraph routing brain
"""

import os
import sys
from typing import TypedDict

_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(_backend, '..', '.env'))
load_dotenv()

# Must be set before torch/ROCm initialises — gfx1100 (RDNA3) requires both.
os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "11.0.0")
os.environ.setdefault("HSA_ENABLE_SDMA", "0")

LLM_PATH = os.environ.get(
    "LLM_MODEL_PATH",
    "/home/james/ml-proj/models/llama-3.1-8b-instruct/snapshots/0e9e39f249a16976918f6564b8830bc894c89659",
)

VALID_ROUTES = {"invoice_extraction", "supplier_query", "combined_verification"}


class MailState(TypedDict):
    pipeline:        str
    email:           dict
    invoice:         dict | None
    confirmed_route: str
    agent_result:    dict
    email_body:      str


class Orchestrator:

    def __init__(self):
        self.llm   = self._load_llm()
        self.graph = self._build_graph()

    def _load_llm(self):
        from langchain_huggingface import HuggingFacePipeline
        from transformers import (
            AutoTokenizer, AutoModelForCausalLM,
            BitsAndBytesConfig, pipeline,
        )
        import torch

        print(f"[orchestrator] Loading model from:\n  {LLM_PATH}")

        tokenizer = AutoTokenizer.from_pretrained(LLM_PATH)

        if torch.cuda.is_available():
            print(f"[orchestrator] GPU available: {torch.cuda.get_device_name(0)}")
            # 4-bit NF4 quantisation: ~4-5 GB VRAM instead of ~15 GB.
            # device_map={"":0} pins everything to cuda:0 without accelerate's
            # multi-device scan that causes hipErrorLaunchFailure on gfx1100.
            quant_cfg = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
            model = AutoModelForCausalLM.from_pretrained(
                LLM_PATH,
                quantization_config=quant_cfg,
                device_map={"":0},
                low_cpu_mem_usage=True,
                attn_implementation="sdpa",
            )
        else:
            print("[orchestrator] No GPU — running on CPU (slow)")
            model = AutoModelForCausalLM.from_pretrained(
                LLM_PATH,
                dtype=torch.float32,
                device_map=None,
                low_cpu_mem_usage=True,
            )

        # Routing needs very few tokens; extraction callers override max_new_tokens.
        hf_pipe = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=32,
            temperature=0.05,
            do_sample=True,
            return_full_text=False,
        )

        print("[orchestrator] Model loaded.")
        return HuggingFacePipeline(pipeline=hf_pipe)

    def _build_graph(self):
        from langgraph.graph import StateGraph, START, END

        g = StateGraph(MailState)
        g.add_node("classify",     self._classify)
        g.add_node("invoice_node", self._invoice_node)
        g.add_node("query_node",   self._query_node)
        g.add_node("format_reply", self._format_reply)

        g.add_edge(START, "classify")
        g.add_conditional_edges(
            "classify",
            lambda s: s["confirmed_route"],
            {
                "invoice_extraction":    "invoice_node",
                "supplier_query":        "query_node",
                "combined_verification": "invoice_node",
            },
        )
        g.add_edge("invoice_node", "format_reply")
        g.add_edge("query_node",   "format_reply")
        g.add_edge("format_reply", END)

        return g.compile()

    def _classify(self, state: MailState) -> dict:
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import StrOutputParser

        hint    = state["pipeline"]
        subject = state["email"].get("subject", "")
        body    = (state["email"].get("body") or "")[:400]
        has_inv = state["invoice"] is not None

        prompt = PromptTemplate.from_template("""\
Classify this email into ONE of these exact route names:
  invoice_extraction
  supplier_query
  combined_verification

Rules:
- invoice_extraction: has attachment, no database question
- supplier_query: text question only, no attachment
- combined_verification: has attachment AND a question

Reply with only the route name. Nothing else.

attachment: {has_inv}
subject: {subject}
body: {body}
hint: {hint}

Route:""")

        chain = prompt | self.llm | StrOutputParser()
        raw   = chain.invoke({"has_inv": has_inv, "subject": subject, "body": body, "hint": hint})
        route = raw.strip().split()[0] if raw.strip() else hint
        if route not in VALID_ROUTES:
            route = hint

        print(f"[classify] hint={hint}  confirmed={route}")
        return {"confirmed_route": route}

    def _invoice_node(self, state: MailState) -> dict:
        from agents.invoice_agent import run as run_invoice
        result = run_invoice(
            invoice=state["invoice"],
            email=state["email"],
            route=state["confirmed_route"],
            llm=self.llm,
        )
        return {"agent_result": result}

    def _query_node(self, state: MailState) -> dict:
        from agents.query_agent import run as run_query
        result = run_query(email=state["email"], llm=self.llm)
        return {"agent_result": result}

    def _format_reply(self, state: MailState) -> dict:
        result = state["agent_result"]
        sender = state["email"].get("from", "sender")

        if "email_body" in result:
            return {"email_body": result["email_body"]}

        status = result.get("status", "unknown")
        route  = state["confirmed_route"]

        if status == "approved":
            body = (
                f"Dear {sender},\n\n"
                f"Invoice {result.get('invoice_number','—')} processed successfully.\n\n"
                f"Supplier : {result.get('supplier','—')}\n"
                f"Total    : {result.get('total_gross_worth','—')}\n\n"
                f"All checks passed. Invoice stored.\n\nRegards,\nAIMailbox"
            )
        elif status == "rejected":
            issues = result.get("failed_checks", ["Validation failure"])
            lines  = "\n".join(f"  • {i}" for i in issues)
            body   = (
                f"Dear {sender},\n\n"
                f"Invoice could not be processed:\n\n{lines}\n\n"
                f"Please correct and resubmit.\n\nRegards,\nAIMailbox"
            )
        elif route == "supplier_query":
            body = (
                f"Dear {sender},\n\n"
                f"{result.get('answer','No result found.')}\n\nRegards,\nAIMailbox"
            )
        else:
            body = str(result)

        return {"email_body": body}

    async def run(self, payload: dict) -> dict:
        pipeline = payload.get("pipeline", "supplier_query")

        state: MailState = {
            "pipeline":        pipeline,
            "email":           payload.get("email", {}),
            "invoice":         payload.get("invoice"),
            "confirmed_route": "",
            "agent_result":    {},
            "email_body":      "",
        }
        final = await self.graph.ainvoke(state)
        agent = final.get("agent_result") or {}
        resp = {
            "status":     agent.get("status", "ok"),
            "pipeline":   final["confirmed_route"],
            "email_body": final["email_body"],
        }
        # Pass through invoice approval fields so the frontend can track them
        if agent.get("invoice_number"):
            resp["invoice_number"] = agent["invoice_number"]
        return resp