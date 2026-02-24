"""
RCL Proto — Shadow-mode policy enforcement service.

Endpoints:
  POST /v1/evaluate     — evaluate event, return verdict, log decision (idempotent)
  GET  /v1/decisions     — query decision log (JSON)
  GET  /v1/rules         — list active rules and thresholds
  GET  /v1/stats         — aggregate stats
  GET  /v1/policy        — current policy (thresholds)
  PUT  /v1/policy        — update policy (thresholds)
  POST /v1/webhook-config — configure alerting (stub, phase 2)
  GET  /               — landing page
  GET  /demo            — interactive scenario demo (React)
  GET  /log             — decision log viewer (HTML)
  GET  /health          — health check
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .rules import evaluate_rules, get_rules_snapshot_from_policy, load_policy
from .schemas import EvaluateRequest, EvaluateResponse, Verdict


# ── Config ──

API_KEY = os.environ.get("RCL_API_KEY", "")  # empty = no auth
START_TS = time.time()

def _get_commit_short() -> str:
    for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "COMMIT_SHA", "SOURCE_VERSION", "RCL_COMMIT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v[:7]
    return "unknown"

def _get_db_path() -> str:
    return getattr(db, "DB_PATH", os.environ.get("RCL_DB_PATH", ""))


# ── Lifespan ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_db()
    yield
    await db.close_db()


# ── App ──

app = FastAPI(
    title="RCL Proto",
    description="Shadow-mode policy enforcement for automated payouts",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

# CORS — explicit origins only
_cors_origins = ["http://localhost:8080", "http://localhost:8000"]
_extra = os.environ.get("RCL_ALLOWED_ORIGINS", "").strip()
if _extra:
    _cors_origins.extend(o.strip() for o in _extra.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "PUT", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ── Auth helper ──

def check_api_key(x_api_key: str | None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Core endpoint (idempotent) ──

@app.post("/v1/evaluate", response_model=EvaluateResponse)
@app.post("/v1/decision", response_model=EvaluateResponse, include_in_schema=False)
async def evaluate_event(req: EvaluateRequest, x_api_key: str | None = Header(default=None)):
    """
    Evaluate a payout event against active policy rules.
    Idempotent: repeat POST with same (tenant, scenario, event_id) returns the original decision.
    """
    check_api_key(x_api_key)

    existing = await db.get_decision_by_event_id(req.event_id, req.tenant, req.scenario)
    if existing:
        return EvaluateResponse(
            event_id=existing["event_id"],
            verdict=Verdict(existing["verdict"]),
            rule_id=existing["rule_id"],
            reason=existing["reason"],
            evaluated_at=datetime.fromisoformat(existing["evaluated_at"]),
        )

    conn = await db.get_db()
    result = await evaluate_rules(
        entity_id=req.entity_id,
        amount=req.amount,
        timestamp=req.timestamp,
        db=conn,
        tenant=req.tenant,
        scenario=req.scenario,
    )

    now = datetime.utcnow()

    if result is None:
        verdict = Verdict.ALLOW
        rule_id = None
        reason = "All rules passed"
        snapshot = None
    else:
        verdict = result.verdict
        rule_id = result.rule_id
        reason = result.reason
        snapshot = json.dumps(result.snapshot)

    await db.insert_decision(
        event_id=req.event_id,
        entity_id=req.entity_id,
        amount=req.amount,
        currency=req.currency,
        event_type=req.event_type,
        event_ts=req.timestamp.isoformat(),
        tenant=req.tenant,
        scenario=req.scenario,
        verdict=verdict.value,
        rule_id=rule_id,
        rule_snapshot=snapshot,
        reason=reason,
        evaluated_at=now.isoformat(),
    )

    return EvaluateResponse(
        event_id=req.event_id,
        verdict=verdict,
        rule_id=rule_id,
        reason=reason,
        evaluated_at=now,
    )


# ── Decision log API ──

@app.get("/v1/decisions")
@app.get("/v1/audit", include_in_schema=False)
async def list_decisions(
    limit: int = Query(default=100, le=1000),
    entity_id: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    scenario: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
):
    check_api_key(x_api_key)
    rows = await db.query_decisions(limit=limit, entity_id=entity_id, verdict=verdict, tenant=tenant, scenario=scenario)
    return {"decisions": rows, "count": len(rows)}


# ── Policy endpoints ──

@app.get("/v1/policy")
async def get_policy(x_api_key: str | None = Header(default=None)):
    """Return current policy (thresholds for all rules)."""
    check_api_key(x_api_key)
    return await db.get_policy()


@app.put("/v1/policy")
async def put_policy(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Update policy. Body: JSON object with rule thresholds.
    Version auto-increments. New events will use updated thresholds.
    """
    check_api_key(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Policy must be a JSON object")
    return await db.update_policy(body)


# ── Rules (derived from current policy) ──

@app.get("/v1/rules")
async def list_rules(x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    conn = await db.get_db()
    policy = await load_policy(conn)
    return {"rules": get_rules_snapshot_from_policy(policy)}


@app.get("/v1/stats")
async def get_stats(
    tenant: str | None = Query(default=None),
    scenario: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
):
    check_api_key(x_api_key)
    return await db.get_stats(tenant=tenant, scenario=scenario)


# ── Health (always public) ──

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "rcl-proto",
        "mode": "shadow",
        "version": app.version,
        "commit": _get_commit_short(),
        "db_path": _get_db_path(),
        "auth_enabled": bool(API_KEY),
        "cors_allowed_origins": os.environ.get("RCL_ALLOWED_ORIGINS", ""),
        "uptime_s": int(time.time() - START_TS),
    }


# ── Point 7: Webhook config stub (phase 2) ──

@app.post("/v1/webhook-config")
async def webhook_config(request: Request, x_api_key: str | None = Header(default=None)):
    """Configure alerting webhooks. Phase 2 — not yet implemented."""
    check_api_key(x_api_key)
    raise HTTPException(
        status_code=501,
        detail="Webhook configuration is planned for the enforcement phase. "
               "Currently RCL operates in shadow mode (observe-only).",
    )


# ═══════════════════════════════════════════════════════════════════
#  HTML Pages — all content inside triple-quoted strings
# ═══════════════════════════════════════════════════════════════════


# ── Point 3 + 5: Landing page with navigation ──

LANDING_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RCL — Risk Control Layer</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07070a;color:#e5e5e5;font-family:'DM Sans',system-ui,sans-serif;min-height:100vh}
a{text-decoration:none}
.nav{border-bottom:1px solid rgba(255,255,255,0.06);padding:0 28px;display:flex;align-items:center;justify-content:space-between;height:52px;background:rgba(255,255,255,0.015)}
.nav-logo{display:flex;align-items:center;gap:10px;color:#e5e5e5}
.nav-icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,#dc2626,#991b1b);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff}
.nav-links{display:flex;gap:2px}
.nav-links a{color:#737373;font-size:12px;font-weight:600;padding:6px 14px;border-radius:5px;transition:all 0.15s}
.nav-links a:hover,.nav-links a.on{color:#e5e5e5;background:rgba(255,255,255,0.05)}
.hero{max-width:800px;margin:0 auto;padding:80px 32px 40px;text-align:center}
.hero h1{font-size:42px;font-weight:700;letter-spacing:-1.5px;line-height:1.15;margin-bottom:16px}
.hero h1 em{font-style:normal;background:linear-gradient(135deg,#f87171,#dc2626);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{font-size:17px;line-height:1.7;color:#a3a3a3;max-width:620px;margin:0 auto 40px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;max-width:900px;margin:0 auto 48px;padding:0 32px}
.card{padding:28px 24px;border-radius:12px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);transition:border-color 0.2s}
.card:hover{border-color:rgba(255,255,255,0.12)}
.card-icon{font-size:22px;margin-bottom:12px}
.card h3{font-size:14px;font-weight:700;margin-bottom:8px;letter-spacing:-0.2px}
.card p{font-size:13px;color:#737373;line-height:1.6}
.cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:60px;padding:0 32px}
.cta a{padding:12px 28px;border-radius:8px;font-size:13px;font-weight:700;letter-spacing:0.3px;transition:all 0.2s;font-family:'JetBrains Mono',monospace}
.cta-primary{background:linear-gradient(135deg,#dc2626,#991b1b);color:#fff;border:none}
.cta-primary:hover{filter:brightness(1.15)}
.cta-secondary{background:transparent;color:#a3a3a3;border:1px solid rgba(255,255,255,0.1)}
.cta-secondary:hover{color:#e5e5e5;border-color:rgba(255,255,255,0.2)}
.foot{text-align:center;padding:40px 32px;border-top:1px solid rgba(255,255,255,0.04);color:#404040;font-size:12px;line-height:1.8}
.tag{display:inline-block;padding:4px 12px;border-radius:100px;font-size:11px;font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:24px}
.tag-shadow{background:rgba(250,204,21,0.08);color:#facc15;border:1px solid rgba(250,204,21,0.15)}
</style></head><body>
<nav class="nav">
  <a href="/" class="nav-logo"><div class="nav-icon">\u2298</div><b style="font-size:14px;letter-spacing:-0.3px">RCL</b><span style="font-size:10px;color:#525252;font-family:'JetBrains Mono',monospace">v0.2</span></a>
  <div class="nav-links">
    <a href="/" class="on">Home</a>
    <a href="/demo">Demo</a>
    <a href="/log">Console</a>
    <a href="/docs">API Docs</a>
  </div>
</nav>

<div class="hero">
  <span class="tag tag-shadow">\u26a1 SHADOW MODE \u00b7 PRE-REVENUE \u00b7 PILOT-READY</span>
  <h1>Financial <em>Circuit Breaker</em> for Automated Operations</h1>
  <p>RCL limits blast radius of operational incidents in automated financial systems. Configurable ceilings, velocity limits, cohort rules \u2014 with an auditable decision log. Shadow mode first, enforcement opt-in.</p>
</div>

<div class="cards">
  <div class="card">
    <div class="card-icon">\u2298</div>
    <h3>Shadow Mode</h3>
    <p>Observe without blocking. RCL evaluates every event and logs a verdict (allow / hold / block) without touching the critical path. Validate rules before enforcement.</p>
  </div>
  <div class="card">
    <div class="card-icon">\u26a1</div>
    <h3>Policy Engine</h3>
    <p>Per-entity ceilings, velocity limits, cohort rules for new vs. established counterparties. Update thresholds via API \u2014 new events use updated policy immediately.</p>
  </div>
  <div class="card">
    <div class="card-icon">&#x1F4CA;</div>
    <h3>Decision Log</h3>
    <p>Every evaluation produces an auditable record: event, verdict, matched rule, reason. Query via API or browse in the console. Designed for compliance and postmortems.</p>
  </div>
</div>

<div class="cta">
  <a href="/demo" class="cta-primary">\u25b6 LIVE DEMO</a>
  <a href="/log" class="cta-secondary">Shadow Console</a>
  <a href="/docs" class="cta-secondary">API Reference</a>
</div>

<div class="foot">
  RCL \u2014 Risk Control Layer \u00b7 Shadow-mode policy enforcement for automated payouts<br>
  All demo data is synthetic, reconstructed from public postmortems. No customer data is used.
</div>
</body></html>"""


# ── Points 2,4,5,6,7,8: Enhanced Console ──

LOG_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RCL — Shadow Console</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07070a;color:#e5e5e5;font-family:'DM Sans',system-ui,sans-serif}
.nav{border-bottom:1px solid rgba(255,255,255,0.06);padding:0 28px;display:flex;align-items:center;justify-content:space-between;height:52px;background:rgba(255,255,255,0.015)}
.nav-logo{display:flex;align-items:center;gap:10px;color:#e5e5e5;text-decoration:none}
.nav-icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,#dc2626,#991b1b);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff}
.nav-links{display:flex;gap:2px}
.nav-links a{text-decoration:none;color:#737373;font-size:12px;font-weight:600;padding:6px 14px;border-radius:5px;transition:all 0.15s}
.nav-links a:hover,.nav-links a.on{color:#e5e5e5;background:rgba(255,255,255,0.05)}
.wrap{padding:24px 28px;max-width:1400px;margin:0 auto}
h1{font-size:20px;font-weight:700;margin-bottom:4px}
.sub{font-size:13px;color:#737373;margin-bottom:24px}
.stats{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
.stat{padding:16px 20px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);min-width:130px}
.stat .label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#737373;margin-bottom:4px;font-weight:600}
.stat .value{font-size:22px;font-weight:700;font-family:'JetBrains Mono',monospace}
.allow{color:#4ade80} .hold{color:#facc15} .block{color:#f87171}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#525252;padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.08)}
td{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,0.03);vertical-align:top}
tr:hover{background:rgba(255,255,255,0.02)}
.badge{padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;display:inline-block}
.badge-allow{background:rgba(74,222,128,0.1);color:#4ade80;border:1px solid rgba(74,222,128,0.2)}
.badge-hold{background:rgba(250,204,21,0.1);color:#facc15;border:1px solid rgba(250,204,21,0.2)}
.badge-block{background:rgba(248,113,113,0.1);color:#f87171;border:1px solid rgba(248,113,113,0.2)}
.mono{font-family:'JetBrains Mono',monospace;font-size:12px;color:#a3a3a3}
.reason{color:#737373;max-width:320px}
.filters{margin-bottom:20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.filters select,.filters input,.filters button{padding:7px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.04);color:#e5e5e5;font-size:12px;font-family:'DM Sans',system-ui,sans-serif}
.filters input{width:120px;font-family:'JetBrains Mono',monospace}
.filters input::placeholder{color:#525252}
.filters button{cursor:pointer;font-weight:600;transition:background 0.15s}
.filters button:hover{background:rgba(255,255,255,0.08)}
.empty{text-align:center;padding:60px;color:#404040;font-size:14px}
.refresh{font-size:12px;color:#525252;margin-top:16px;text-align:right;font-family:'JetBrains Mono',monospace}
.tabs{display:flex;gap:0;margin-bottom:24px;border-bottom:1px solid rgba(255,255,255,0.08)}
.tab{padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;color:#737373;border-bottom:2px solid transparent;transition:all 0.2s}
.tab.active{color:#e5e5e5;border-bottom-color:#f87171}
.tab:hover{color:#a3a3a3}
.panel{display:none}.panel.active{display:block}
textarea{width:100%;min-height:280px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e5e5e5;font-family:'JetBrains Mono',monospace;font-size:13px;padding:16px;resize:vertical;line-height:1.6}
.policy-bar{display:flex;gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap}
.policy-bar button,.btn-sm{padding:8px 20px;border-radius:6px;border:none;font-size:13px;font-weight:700;cursor:pointer;transition:all 0.2s}
.btn-save{background:rgba(74,222,128,0.15);color:#4ade80}
.btn-save:hover{background:rgba(74,222,128,0.25)}
.btn-load{background:rgba(139,92,246,0.15);color:#c4b5fd}
.btn-load:hover{background:rgba(139,92,246,0.25)}
.btn-default{background:rgba(255,255,255,0.04);color:#a3a3a3;border:1px solid rgba(255,255,255,0.08)}
.btn-default:hover{background:rgba(255,255,255,0.08)}
.policy-msg{font-size:12px;font-family:'JetBrains Mono',monospace}
.policy-ver{font-size:12px;color:#525252;font-family:'JetBrains Mono',monospace}
.key-bar{display:flex;gap:8px;align-items:center;margin-bottom:20px;padding:12px 16px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);flex-wrap:wrap}
.key-bar label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#737373;font-weight:600}
.key-bar input{padding:6px 10px;border-radius:4px;border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.04);color:#e5e5e5;font-family:'JetBrains Mono',monospace;font-size:12px;width:200px}
.key-bar button{padding:6px 14px;border-radius:4px;border:none;background:rgba(139,92,246,0.15);color:#c4b5fd;font-size:12px;font-weight:700;cursor:pointer}
.key-bar button:hover{background:rgba(139,92,246,0.25)}
.key-bar .key-msg{font-size:11px;font-family:'JetBrains Mono',monospace}
.auth-err{padding:12px 16px;border-radius:8px;background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.25);color:#f87171;font-size:13px;margin-bottom:16px;display:none}
.copy-btn{padding:5px 14px;border-radius:5px;border:1px solid rgba(139,92,246,0.25);background:rgba(139,92,246,0.1);color:#c4b5fd;font-size:11px;font-weight:700;cursor:pointer;font-family:'JetBrains Mono',monospace;transition:all 0.15s;white-space:nowrap}
.copy-btn:hover{background:rgba(139,92,246,0.2);border-color:rgba(139,92,246,0.4)}
.rule-card{padding:18px 20px;border-radius:8px;border:1px solid rgba(139,92,246,0.15);background:rgba(139,92,246,0.04);flex:1 1 260px}
.rule-card h4{font-size:13px;font-weight:700;color:#c4b5fd;font-family:'JetBrains Mono',monospace;margin-bottom:4px}
.rule-card .rtype{font-size:11px;color:#737373;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px}
.rule-card p{font-size:12px;color:#a3a3a3;line-height:1.5}
.rule-card .thresh{margin-top:8px;font-size:11px;color:#facc15;font-family:'JetBrains Mono',monospace}
.coming-soon{padding:32px;border-radius:10px;border:1px dashed rgba(255,255,255,0.08);text-align:center}
.coming-soon h3{font-size:14px;color:#737373;margin-bottom:8px}
.coming-soon p{font-size:13px;color:#525252;line-height:1.6;max-width:480px;margin:0 auto}
</style></head><body>

<nav class="nav">
  <a href="/" class="nav-logo"><div class="nav-icon">\u2298</div><b style="font-size:14px;letter-spacing:-0.3px">RCL</b><span style="font-size:10px;color:#525252;font-family:'JetBrains Mono',monospace">v0.2</span></a>
  <div class="nav-links">
    <a href="/">Home</a>
    <a href="/demo">Demo</a>
    <a href="/log" class="on">Console</a>
    <a href="/docs">API Docs</a>
  </div>
</nav>

<div class="wrap">
<h1>\u2298 RCL \u2014 Shadow Mode Console</h1>
<div class="sub">Decision log \u00b7 Policy editor \u00b7 Rules \u00b7 Read/write via API</div>

<!-- Auth -->
<div class="key-bar">
  <label>API Key</label>
  <input type="password" id="apiKeyInput" placeholder="leave empty if auth disabled" />
  <button onclick="saveKey()">Save</button>
  <span class="key-msg" id="keyMsg"></span>
</div>
<div class="auth-err" id="authErr">\u26a0 401 Unauthorized \u2014 check your API key above.</div>

<!-- Tabs (Point 4,7: added Rules + Alerting) -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('log')">Decision Log</div>
  <div class="tab" onclick="switchTab('policy')">Policy</div>
  <div class="tab" onclick="switchTab('rules')">Rules</div>
  <div class="tab" onclick="switchTab('alerting')">Alerting</div>
</div>

<!-- LOG PANEL -->
<div class="panel active" id="panel-log">
  <div class="stats" id="stats"></div>

  <!-- Point 8: tenant/scenario filters -->
  <div class="filters">
    <select id="fVerdict" onchange="loadLog()">
      <option value="">All verdicts</option>
      <option value="allow">Allow</option>
      <option value="hold-for-review">Hold for review</option>
      <option value="block">Block</option>
    </select>
    <input id="fTenant" type="text" placeholder="tenant" onchange="loadLog()" />
    <input id="fScenario" type="text" placeholder="scenario" onchange="loadLog()" />
    <button onclick="loadLog()">\u21bb Refresh</button>
  </div>

  <details style="margin:0 0 16px 0;padding:12px 16px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02)">
    <summary style="cursor:pointer;font-size:13px;color:#a3a3a3;font-weight:600">Integration snippets (copy/paste)</summary>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <!-- Point 2: fixed style on copy buttons -->
      <button class="copy-btn" type="button" data-copy="snipDecision">Copy: decision</button>
      <button class="copy-btn" type="button" data-copy="snipAudit">Copy: audit</button>
      <button class="copy-btn" type="button" data-copy="snipPolicy">Copy: policy update</button>
    </div>
    <pre id="snipDecision" class="mono" style="white-space:pre-wrap;margin-top:12px;padding:12px;background:rgba(0,0,0,0.3);border-radius:6px;border:1px solid rgba(255,255,255,0.04)"></pre>
    <pre id="snipAudit" class="mono" style="white-space:pre-wrap;margin-top:8px;padding:12px;background:rgba(0,0,0,0.3);border-radius:6px;border:1px solid rgba(255,255,255,0.04)"></pre>
    <pre id="snipPolicy" class="mono" style="white-space:pre-wrap;margin-top:8px;padding:12px;background:rgba(0,0,0,0.3);border-radius:6px;border:1px solid rgba(255,255,255,0.04)"></pre>
  </details>

  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>#</th><th>Time</th><th>Tenant</th><th>Scenario</th><th>Entity</th><th>Amount</th><th>Verdict</th><th>Rule</th><th>Reason</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  </div>
  <div id="empty" class="empty" style="display:none">No decisions yet. Send events to POST /v1/evaluate</div>
  <div class="refresh" id="ts"></div>
</div>

<!-- POLICY PANEL (Point 6: improved editor) -->
<div class="panel" id="panel-policy">
  <div style="margin-bottom:12px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <span class="policy-ver" id="polVer"></span>
  </div>
  <textarea id="polEditor" spellcheck="false" placeholder='Loading policy... Click "Load current" to fetch from API.'></textarea>
  <div class="policy-bar">
    <button class="btn-load" onclick="loadPolicy()">Load current</button>
    <button class="btn-default" onclick="loadDefaults()">Reset to defaults</button>
    <button class="btn-save" onclick="savePolicy()">Save to server</button>
    <span class="policy-msg" id="polMsg"></span>
  </div>
  <div style="margin-top:16px;padding:14px 18px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04)">
    <div style="font-size:11px;color:#525252;text-transform:uppercase;letter-spacing:1px;font-weight:600;margin-bottom:8px">Policy structure reference</div>
    <pre class="mono" style="font-size:11px;color:#737373;line-height:1.5;white-space:pre-wrap">{
  "R-CEIL":  { "daily_limit": 500000, "action": "block" },
  "R-VEL":   { "max_tx_per_hour": 50, "window_hours": 1, "action": "hold-for-review" },
  "R-COHORT": { "block_threshold": 100000, "hold_threshold": 50000, "new_entity_days": 30 }
}</pre>
  </div>
</div>

<!-- RULES PANEL (Point 4) -->
<div class="panel" id="panel-rules">
  <div style="margin-bottom:16px;display:flex;align-items:center;gap:12px">
    <span style="font-size:13px;color:#737373">Active rules derived from current policy</span>
    <button class="btn-load" onclick="loadRules()" style="padding:6px 14px;font-size:12px">\u21bb Reload</button>
  </div>
  <div id="rulesContainer" style="display:flex;gap:14px;flex-wrap:wrap"></div>
  <div id="rulesEmpty" class="empty" style="display:none">No rules loaded. Click Reload or check API key.</div>
</div>

<!-- ALERTING PANEL (Point 7: stub) -->
<div class="panel" id="panel-alerting">
  <div class="coming-soon">
    <div style="font-size:32px;margin-bottom:12px">&#x1F514;</div>
    <h3>Alerting & Enforcement \u2014 Phase 2</h3>
    <p>Webhook notifications and inline enforcement are planned for the next phase.<br>
    Currently RCL operates in <strong style="color:#facc15">shadow mode</strong> (observe-only).</p>
    <div style="margin-top:20px;display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
      <div style="padding:12px 18px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);text-align:left">
        <div style="font-size:11px;color:#525252;text-transform:uppercase;font-weight:600;margin-bottom:4px">Planned</div>
        <div style="font-size:13px;color:#a3a3a3">Slack / webhook on hold/block verdicts</div>
      </div>
      <div style="padding:12px 18px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);text-align:left">
        <div style="font-size:11px;color:#525252;text-transform:uppercase;font-weight:600;margin-bottom:4px">Planned</div>
        <div style="font-size:13px;color:#a3a3a3">Inline gating (opt-in enforcement)</div>
      </div>
      <div style="padding:12px 18px;border-radius:8px;border:1px solid rgba(255,255,255,0.06);background:rgba(255,255,255,0.02);text-align:left">
        <div style="font-size:11px;color:#525252;text-transform:uppercase;font-weight:600;margin-bottom:4px">Planned</div>
        <div style="font-size:13px;color:#a3a3a3">Email digest (daily summary)</div>
      </div>
    </div>
    <div style="margin-top:20px"><code class="mono" style="font-size:11px;color:#525252">POST /v1/webhook-config \u2192 501 Not Implemented</code></div>
  </div>
</div>
</div><!-- /wrap -->

<script>
// ── API key ──
function getKey(){return localStorage.getItem('rcl_api_key')||'';}
function saveKey(){
  var v=document.getElementById('apiKeyInput').value.trim();
  if(v){localStorage.setItem('rcl_api_key',v);}else{localStorage.removeItem('rcl_api_key');}
  var m=document.getElementById('keyMsg');
  m.textContent=v?'Saved \u2713':'Cleared';m.style.color='#4ade80';
  document.getElementById('authErr').style.display='none';
  loadLog();
}
document.getElementById('apiKeyInput').value=getKey();

function apiFetch(url,opts){
  opts=opts||{};
  var key=getKey();
  if(key){opts.headers=opts.headers||{};opts.headers['X-API-Key']=key;}
  return fetch(url,opts).then(function(r){
    if(r.status===401){document.getElementById('authErr').style.display='block';throw new Error('401');}
    document.getElementById('authErr').style.display='none';
    return r;
  });
}

// ── Tabs (Point 4,7: added rules, alerting) ──
function switchTab(name){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.textContent.trim().toLowerCase().includes(name));});
  document.querySelectorAll('.panel').forEach(function(p){p.classList.remove('active');});
  document.getElementById('panel-'+name).classList.add('active');
  if(name==='policy')loadPolicy();
  if(name==='log')loadLog();
  if(name==='rules')loadRules();
}

// ── Decision log (Point 8: tenant/scenario filters) ──
function loadLog(){
  var parts=[];
  var v=document.getElementById('fVerdict').value;
  var t=document.getElementById('fTenant').value.trim();
  var s=document.getElementById('fScenario').value.trim();
  if(v)parts.push('verdict='+encodeURIComponent(v));
  if(t)parts.push('tenant='+encodeURIComponent(t));
  if(s)parts.push('scenario='+encodeURIComponent(s));
  parts.push('limit=200');
  var q='?'+parts.join('&');

  var statsQ=t||s?'?'+(t?'tenant='+encodeURIComponent(t)+'&':'')+(s?'scenario='+encodeURIComponent(s):''):'';

  Promise.all([apiFetch('/v1/decisions'+q),apiFetch('/v1/stats'+statsQ)])
  .then(function(res){return Promise.all([res[0].json(),res[1].json()]);})
  .then(function(data){
    var d=data[0],s=data[1];
    document.getElementById('stats').innerHTML=
      '<div class="stat"><div class="label">Total</div><div class="value">'+( s.total||0)+'</div></div>'+
      '<div class="stat"><div class="label">Allow</div><div class="value allow">'+(s.allow_count||0)+'</div></div>'+
      '<div class="stat"><div class="label">Hold</div><div class="value hold">'+(s.hold_count||0)+'</div></div>'+
      '<div class="stat"><div class="label">Block</div><div class="value block">'+(s.block_count||0)+'</div></div>'+
      '<div class="stat"><div class="label">Blocked $</div><div class="value block">$'+(s.blocked_amount||0).toLocaleString()+'</div></div>';
    var rows=d.decisions||[];
    if(!rows.length){document.getElementById('tbody').innerHTML='';document.getElementById('empty').style.display='block';return;}
    document.getElementById('empty').style.display='none';
    var cls={'allow':'badge-allow','hold-for-review':'badge-hold','block':'badge-block'};
    document.getElementById('tbody').innerHTML=rows.map(function(r){return '<tr>'+
      '<td class="mono">'+r.id+'</td>'+
      '<td class="mono">'+(r.evaluated_at||'').replace('T',' ').slice(0,19)+'</td>'+
      '<td class="mono">'+(r.tenant||'demo')+'</td>'+
      '<td class="mono">'+(r.scenario||'default')+'</td>'+
      '<td class="mono">'+r.entity_id+'</td>'+
      '<td class="mono">$'+Number(r.amount).toLocaleString(undefined,{minimumFractionDigits:2})+'</td>'+
      '<td><span class="badge '+(cls[r.verdict]||'')+'">'+r.verdict+'</span></td>'+
      '<td class="mono">'+(r.rule_id||'\u2014')+'</td>'+
      '<td class="reason">'+(r.reason||'')+'</td></tr>';
    }).join('');
    document.getElementById('ts').textContent='Last refresh: '+new Date().toLocaleTimeString();
  }).catch(function(e){if(e.message!=='401')console.error(e);});
}

// ── Policy (Point 6: defaults) ──
var DEFAULT_POLICY='{\\n  "R-CEIL": { "daily_limit": 500000, "action": "block" },\\n  "R-VEL": { "max_tx_per_hour": 50, "window_hours": 1, "action": "hold-for-review" },\\n  "R-COHORT": { "block_threshold": 100000, "hold_threshold": 50000, "new_entity_days": 30 }\\n}';

function loadPolicy(){
  apiFetch('/v1/policy').then(function(r){return r.json();}).then(function(d){
    document.getElementById('polEditor').value=JSON.stringify(d.policy,null,2);
    document.getElementById('polVer').textContent='version '+d.version+' \u00b7 updated '+d.updated_at;
    document.getElementById('polMsg').textContent='';
    document.getElementById('polMsg').style.color='';
  }).catch(function(e){
    if(e.message!=='401'){document.getElementById('polMsg').textContent='Error loading policy';document.getElementById('polMsg').style.color='#f87171';}
  });
}

function loadDefaults(){
  document.getElementById('polEditor').value=DEFAULT_POLICY;
  document.getElementById('polMsg').textContent='Defaults loaded (not saved yet)';
  document.getElementById('polMsg').style.color='#facc15';
}

function savePolicy(){
  var msg=document.getElementById('polMsg');
  var parsed;
  try{parsed=JSON.parse(document.getElementById('polEditor').value);}
  catch(e){msg.textContent='Invalid JSON \u2014 check syntax';msg.style.color='#f87171';return;}
  apiFetch('/v1/policy',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(parsed)})
  .then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
  .then(function(d){
    document.getElementById('polVer').textContent='version '+d.version+' \u00b7 updated '+d.updated_at;
    msg.textContent='Saved \u2713';msg.style.color='#4ade80';
  }).catch(function(e){
    if(e.message!=='401'){msg.textContent='Error: '+e.message;msg.style.color='#f87171';}
  });
}

// ── Rules (Point 4) ──
function loadRules(){
  apiFetch('/v1/rules').then(function(r){return r.json();}).then(function(d){
    var rules=d.rules||[];
    if(!rules.length){document.getElementById('rulesContainer').innerHTML='';document.getElementById('rulesEmpty').style.display='block';return;}
    document.getElementById('rulesEmpty').style.display='none';
    var icons={'ceiling':'\u2298','velocity':'\u26a1','cohort':'\\ud83d\\udc65','drift':'\u2195'};
    document.getElementById('rulesContainer').innerHTML=rules.map(function(r){
      var icon=icons[r.type]||'\u2022';
      var thresholds=r.thresholds?Object.keys(r.thresholds).map(function(k){return k+': '+r.thresholds[k];}).join(' \u00b7 '):'';
      return '<div class="rule-card">'+
        '<h4>'+icon+' '+r.id+'</h4>'+
        '<div class="rtype">'+( r.type||'rule')+'</div>'+
        '<p>'+(r.description||r.name||'')+'</p>'+
        (thresholds?'<div class="thresh">'+thresholds+'</div>':'')+
      '</div>';
    }).join('');
  }).catch(function(e){
    if(e.message!=='401'){document.getElementById('rulesContainer').innerHTML='<div class="empty">Error loading rules</div>';}
  });
}

// ── Snippets ──
function populateSnippets(){
  var base=window.location.origin;
  var el;
  el=document.getElementById('snipDecision');
  if(el)el.textContent='curl -s -X POST '+base+'/v1/decision \\\n  -H "Content-Type: application/json" \\\n  -H "X-API-Key: <key>" \\\n  -d \\'{"event_id":"evt_1","tenant":"demo","scenario":"v1","entity_id":"partner_alpha","amount":1500,"event_type":"payout"}\\'';
  el=document.getElementById('snipAudit');
  if(el)el.textContent='curl -s "'+base+'/v1/audit?limit=20&tenant=demo&scenario=v1" \\\n  -H "X-API-Key: <key>"';
  el=document.getElementById('snipPolicy');
  if(el)el.textContent='curl -s -X PUT '+base+'/v1/policy \\\n  -H "Content-Type: application/json" \\\n  -H "X-API-Key: <key>" \\\n  -d @policy.json';
}

// ── Copy (3-tier with feedback) ──
function copySnippet(preId,btn){
  var el=document.getElementById(preId);
  var txt=(el&&el.textContent)?el.textContent.trim():'';
  if(!txt){btn.textContent='Empty!';setTimeout(function(){btn.textContent=btn._orig;},900);return;}
  var orig=btn._orig||btn.textContent;
  btn._orig=orig;
  btn.textContent='Copying\u2026';
  if(navigator.clipboard&&navigator.clipboard.writeText&&window.isSecureContext){
    navigator.clipboard.writeText(txt).then(function(){
      btn.textContent='Copied \u2713';setTimeout(function(){btn.textContent=orig;},900);
    }).catch(function(){fallbackCopy(txt,btn,orig);});
    return;
  }
  fallbackCopy(txt,btn,orig);
}
function fallbackCopy(txt,btn,orig){
  var ok=false;
  try{var ta=document.createElement('textarea');ta.value=txt;ta.setAttribute('readonly','');
  ta.style.cssText='position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.appendChild(ta);ta.select();ta.setSelectionRange(0,txt.length);
  ok=document.execCommand('copy');document.body.removeChild(ta);}catch(e){ok=false;}
  if(ok){btn.textContent='Copied \u2713';setTimeout(function(){btn.textContent=orig;},900);return;}
  window.prompt('Copy with Ctrl+C / Cmd+C, then close:',txt);
  btn.textContent='Manual copy \u2713';setTimeout(function(){btn.textContent=orig;},900);
}

// ── Init ──
(function(){
  function bind(){
    var btns=document.querySelectorAll('button[data-copy]');
    for(var i=0;i<btns.length;i++){
      var btn=btns[i];
      if(btn.__bound)continue;
      btn.__bound=true;
      btn._orig=btn.textContent;
      btn.addEventListener('click',(function(b){
        return function(e){e.preventDefault();copySnippet(b.getAttribute('data-copy'),b);};
      })(btn));
    }
  }
  function init(){
    populateSnippets();
    bind();
    try{loadLog();}catch(e){}
    if(!window.__logTimer)window.__logTimer=setInterval(function(){try{loadLog();}catch(e){}},5000);
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',init);}
  else{init();}
})();
</script>
</body></html>"""


# ── Point 1: Demo page served at /demo ──

DEMO_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RCL — Live Demo</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.9/babel.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e5e5e5;font-family:'DM Sans','Segoe UI',system-ui,sans-serif}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:3px}
button:hover{filter:brightness(1.2)}
@keyframes rcl-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.demo-nav{border-bottom:1px solid rgba(255,255,255,0.06);padding:0 28px;display:flex;align-items:center;justify-content:space-between;height:52px;background:rgba(255,255,255,0.015)}
.demo-nav a{text-decoration:none}
.demo-nav-logo{display:flex;align-items:center;gap:10px;color:#e5e5e5}
.demo-nav-icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,#dc2626,#991b1b);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff}
.demo-nav-links{display:flex;gap:2px}
.demo-nav-links a{color:#737373;font-size:12px;font-weight:600;padding:6px 14px;border-radius:5px;transition:all 0.15s}
.demo-nav-links a:hover,.demo-nav-links a.on{color:#e5e5e5;background:rgba(255,255,255,0.05)}
</style></head><body>

<nav class="demo-nav">
  <a href="/" class="demo-nav-logo"><div class="demo-nav-icon">\u2298</div><b style="font-size:14px;letter-spacing:-0.3px">RCL</b><span style="font-size:10px;color:#525252;font-family:'JetBrains Mono',monospace">v0.2</span></a>
  <div class="demo-nav-links">
    <a href="/">Home</a>
    <a href="/demo" class="on">Demo</a>
    <a href="/log">Console</a>
    <a href="/docs">API Docs</a>
  </div>
</nav>

<div id="root"></div>
<script type="text/babel">
const {useState,useEffect,useRef,useCallback}=React;

const DEFAULT_API_BASE="";

const SCENARIOS={
  synapse:{
    id:"synapse",title:"Synapse \u2014 $160M Frozen Funds",
    subtitle:"BaaS platform, ledger mismatch \u2192 mass payout failures",
    description:"Synapse's ledger diverged from partner banks. Automated payouts continued executing against stale balances, amplifying the mismatch until $160M was frozen across 100+ fintech programs.",
    blastRadiusReal:"$160M frozen",timeToDetect:"~weeks (discovered during audit)",
    rules:[
      {id:"R-CEIL",name:"Daily exposure ceiling",description:"Aggregate outbound per entity per 24h",type:"ceiling"},
      {id:"R-VEL",name:"Velocity spike",description:"Tx count per entity per window",type:"velocity"},
      {id:"R-COHORT",name:"Single-tx anomaly",description:"Hold/block if single tx exceeds threshold",type:"ceiling"},
    ],
    events:[
      {id:1,ts:"09:01:12",entity:"program_047",amount:14200},
      {id:2,ts:"09:03:45",entity:"program_047",amount:8900},
      {id:3,ts:"09:12:33",entity:"program_112",amount:340000},
      {id:4,ts:"09:45:01",entity:"program_047",amount:1250000},
      {id:5,ts:"10:02:17",entity:"program_112",amount:890000},
      {id:6,ts:"10:15:44",entity:"program_047",amount:2100000},
      {id:7,ts:"10:22:08",entity:"program_203",amount:67000},
      {id:8,ts:"11:30:55",entity:"program_112",amount:1800000},
      {id:9,ts:"12:01:03",entity:"program_047",amount:450000},
      {id:10,ts:"13:15:22",entity:"program_112",amount:3200000},
      {id:11,ts:"14:00:00",entity:"program_047",amount:780000},
    ],
  },
  compound:{
    id:"compound",title:"Compound \u2014 Uncapped COMP Distribution",
    subtitle:"DeFi protocol, config bug \u2192 $80M+ overclaimed",
    description:"A governance proposal introduced a bug in Compound's COMP token distribution. Users could claim far more tokens than intended. The team had no circuit breaker to pause claims \u2014 took 7 days to push a fix through governance.",
    blastRadiusReal:"$80M+ overclaimed",timeToDetect:"~hours (community spotted anomalies)",
    rules:[
      {id:"R-CEIL",name:"Daily exposure ceiling",description:"Aggregate outbound per entity per 24h",type:"ceiling"},
      {id:"R-VEL",name:"Velocity spike",description:"Tx count per entity per window",type:"velocity"},
      {id:"R-COHORT",name:"Single-tx anomaly",description:"Hold/block if single tx exceeds threshold",type:"ceiling"},
    ],
    events:[
      {id:1,ts:"08:00:15",entity:"0x7a3f_e1c2",amount:1200},
      {id:2,ts:"08:04:33",entity:"0x9b2d_f4a8",amount:3400},
      {id:3,ts:"08:12:07",entity:"0x1c8e_b3d5",amount:89000},
      {id:4,ts:"08:15:44",entity:"0x4f6a_c7e9",amount:142000},
      {id:5,ts:"08:22:11",entity:"0x1c8e_b3d5",amount:234000},
      {id:6,ts:"08:30:00",entity:"0x2e5b_a1f3",amount:67000},
      {id:7,ts:"08:33:18",entity:"0x8d4c_e6b2",amount:312000},
      {id:8,ts:"08:45:02",entity:"0x7a3f_e1c2",amount:1500},
      {id:9,ts:"09:01:30",entity:"0x5f9d_b8c4",amount:890000},
    ],
  },
  clerk:{
    id:"clerk",title:"Clerk \u2014 Blast Radius Expansion",
    subtitle:"Auth platform, config change \u2192 cascading failures",
    description:"A configuration change at Clerk cascaded across their multi-tenant platform. What started as a single-tenant issue expanded to affect multiple customers because no blast radius containment was in place for config propagation.",
    blastRadiusReal:"Multi-tenant cascading outage",timeToDetect:"~30 min (customer reports)",
    rules:[
      {id:"R-CEIL",name:"Daily exposure ceiling",description:"Aggregate outbound per entity per 24h",type:"ceiling"},
      {id:"R-VEL",name:"Velocity spike",description:"Tx count per entity per window",type:"velocity"},
      {id:"R-COHORT",name:"Single-tx anomaly",description:"Hold/block if single tx exceeds threshold",type:"ceiling"},
    ],
    events:[
      {id:1,ts:"14:00:05",entity:"tenant_acme",amount:1100},
      {id:2,ts:"14:00:08",entity:"tenant_acme",amount:2200},
      {id:3,ts:"14:00:12",entity:"tenant_acme",amount:47000},
      {id:4,ts:"14:00:15",entity:"tenant_beta",amount:1500},
      {id:5,ts:"14:00:18",entity:"tenant_gamma",amount:3300},
      {id:6,ts:"14:00:22",entity:"tenant_acme",amount:800},
      {id:7,ts:"14:01:00",entity:"tenant_delta",amount:28000},
      {id:8,ts:"14:02:15",entity:"tenant_acme",amount:500},
    ],
  },
};

const VS={
  allow:{bg:"rgba(34,197,94,0.08)",border:"#166534",badge:"#14532d",badgeText:"#86efac"},
  "hold-for-review":{bg:"rgba(234,179,8,0.08)",border:"#854d0e",badge:"#713f12",badgeText:"#fde047"},
  block:{bg:"rgba(239,68,68,0.08)",border:"#991b1b",badge:"#7f1d1d",badgeText:"#fca5a5"},
};
const FB={bg:"transparent",border:"#333",badge:"#333",badgeText:"#999"};
const TI={ceiling:"\u2298",velocity:"\u26a1",drift:"\u2195"};

function fmtAmt(a){if(a===0)return"\u2014";return"$"+a.toLocaleString("en-US");}

function Badge({verdict}){
  const s=VS[verdict]||FB;
  return <span style={{display:"inline-block",padding:"2px 10px",borderRadius:4,fontSize:11,fontWeight:700,letterSpacing:"0.5px",textTransform:"uppercase",background:s.badge,color:s.badgeText,fontFamily:"'JetBrains Mono',monospace"}}>{verdict||"?"}</span>;
}

function RuleChip({ruleId,rules}){
  if(!ruleId)return <span style={{color:"#525252",fontSize:12,fontFamily:"monospace"}}>\u2014</span>;
  const r=rules.find(x=>x.id===ruleId);
  const icon=r?(TI[r.type]||""):"";
  return <span style={{display:"inline-flex",alignItems:"center",gap:4,padding:"2px 8px",borderRadius:4,fontSize:11,fontWeight:600,background:"rgba(139,92,246,0.15)",color:"#c4b5fd",fontFamily:"'JetBrains Mono',monospace"}}>{icon} {ruleId}</span>;
}

function Stat({label,value,color,sub}){
  return(
    <div style={{flex:1,minWidth:140,background:"rgba(255,255,255,0.02)",border:"1px solid rgba(255,255,255,0.06)",borderRadius:8,padding:"16px 20px"}}>
      <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1px",color:"#737373",marginBottom:6,fontWeight:600}}>{label}</div>
      <div style={{fontSize:24,fontWeight:700,color,fontFamily:"'JetBrains Mono',monospace",lineHeight:1.2}}>{value}</div>
      {sub?<div style={{fontSize:12,color:"#737373",marginTop:4}}>{sub}</div>:null}
    </div>
  );
}

function StatusDot({status}){
  const colors={ok:"#4ade80",error:"#f87171",checking:"#facc15",unknown:"#525252"};
  const labels={ok:"API connected",error:"API offline",checking:"Checking\u2026",unknown:"Not checked"};
  return(
    <div style={{display:"flex",alignItems:"center",gap:6}}>
      <div style={{width:8,height:8,borderRadius:"50%",background:colors[status]||colors.unknown,boxShadow:status==="ok"?"0 0 6px rgba(74,222,128,0.4)":"none"}}/>
      <span style={{fontSize:11,color:colors[status]||colors.unknown,fontFamily:"'JetBrains Mono',monospace"}}>{labels[status]||"Unknown"}</span>
    </div>
  );
}

function App(){
  const[activeId,setActiveId]=useState("synapse");
  const[events,setEvents]=useState([]);
  const[playing,setPlaying]=useState(false);
  const[done,setDone]=useState(false);
  const[picked,setPicked]=useState(null);
  const[apiStatus,setApiStatus]=useState("unknown");
  const[apiError,setApiError]=useState(null);
  const[apiBase,setApiBase]=useState(()=>localStorage.getItem("rcl_api_base")||DEFAULT_API_BASE);
  const[apiKey,setApiKey]=useState(()=>localStorage.getItem("rcl_api_key")||"");
  const[runId,setRunId]=useState(()=>Math.random().toString(36).slice(2,8));
  const playingRef=useRef(false);
  const idxRef=useRef(0);
  const logBox=useRef(null);
  const sc=SCENARIOS[activeId];

  useEffect(()=>{localStorage.setItem("rcl_api_base",apiBase);},[apiBase]);
  useEffect(()=>{localStorage.setItem("rcl_api_key",apiKey);},[apiKey]);

  const checkHealth=useCallback(async()=>{
    setApiStatus("checking");
    try{
      const base=apiBase.replace(/\\/$/,"")||window.location.origin;
      const r=await fetch(base+"/health",{signal:AbortSignal.timeout(3000)});
      if(r.ok){setApiStatus("ok");setApiError(null);}
      else{setApiStatus("error");setApiError("Health returned "+r.status);}
    }catch(e){setApiStatus("error");setApiError("Cannot reach API");}
  },[apiBase]);

  useEffect(()=>{checkHealth();},[checkHealth]);

  const reset=useCallback(()=>{
    playingRef.current=false;idxRef.current=0;
    setEvents([]);setPlaying(false);setDone(false);setPicked(null);setApiError(null);
    setRunId(Math.random().toString(36).slice(2,8));
  },[]);

  useEffect(()=>{reset();},[activeId,reset]);

  const tick=useCallback(async()=>{
    if(!playingRef.current)return;
    const src=sc.events;const idx=idxRef.current;
    if(idx>=src.length){playingRef.current=false;setPlaying(false);setDone(true);return;}
    const raw=src[idx];idxRef.current=idx+1;
    const eventId=activeId+"_"+runId+"_"+raw.id;
    const base=(apiBase.replace(/\\/$/,"")||window.location.origin);
    const headers={"Content-Type":"application/json"};
    if(apiKey)headers["X-API-Key"]=apiKey;
    try{
      const r=await fetch(base+"/v1/decision",{
        method:"POST",headers,
        body:JSON.stringify({event_id:eventId,entity_id:raw.entity,amount:raw.amount,event_type:"payout"}),
        signal:AbortSignal.timeout(5000),
      });
      if(r.status===401){setApiError("401 Unauthorized");playingRef.current=false;setPlaying(false);return;}
      if(!r.ok){setApiError("API error: "+r.status);playingRef.current=false;setPlaying(false);return;}
      const d=await r.json();
      setApiError(null);
      setEvents(prev=>[...prev,{id:raw.id,ts:raw.ts,entity:raw.entity,amount:raw.amount,verdict:d.verdict,rule:d.rule_id||null,note:d.reason||""}]);
      if(playingRef.current)setTimeout(tick,700);
    }catch(e){setApiError("Network error");playingRef.current=false;setPlaying(false);}
  },[sc,activeId,runId,apiBase,apiKey]);

  function handlePlay(){
    if(playing){playingRef.current=false;setPlaying(false);return;}
    if(events.length>=sc.events.length){setEvents([]);setDone(false);setPicked(null);idxRef.current=0;setRunId(Math.random().toString(36).slice(2,8));}
    setApiError(null);playingRef.current=true;setPlaying(true);setTimeout(tick,100);
  }

  useEffect(()=>{if(logBox.current)logBox.current.scrollTop=logBox.current.scrollHeight;},[events]);

  const safe=events.filter(Boolean);
  const counts={allow:0,"hold-for-review":0,block:0};
  safe.forEach(e=>{if(e.verdict in counts)counts[e.verdict]++;});
  const blocked$=safe.filter(e=>e.verdict==="block").reduce((s,e)=>s+(e.amount||0),0);
  const gridCols="70px 120px 100px 130px 80px 1fr";
  const btnLabel=playing?"\u23f8 PAUSE":safe.length>0&&safe.length<sc.events.length?"\u25b6 RESUME":safe.length>=sc.events.length?"\u21bb REPLAY":"\u25b6 RUN SIMULATION";
  const summaryPrevented=blocked$>0?"$"+blocked$.toLocaleString()+" flagged/blocked":"No events blocked";
  const holdCount=counts["hold-for-review"];const blockCount=counts.block;

  return(
    <div style={{minHeight:"100vh",padding:0}}>
      <div style={{borderBottom:"1px solid rgba(255,255,255,0.06)",padding:"16px 32px",display:"flex",alignItems:"center",justifyContent:"space-between",flexWrap:"wrap",gap:12}}>
        <div style={{display:"flex",alignItems:"center",gap:16}}>
          <div style={{fontSize:16,fontWeight:700,letterSpacing:"-0.3px"}}>Live Scenario Demo</div>
          <div style={{fontSize:12,color:"#737373"}}>Shadow mode \u00b7 Verdicts from live API</div>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:16}}>
          <StatusDot status={apiStatus}/>
          <div style={{fontSize:11,color:"#525252",fontFamily:"'JetBrains Mono',monospace"}}>Synthetic data</div>
        </div>
      </div>

      <div style={{borderBottom:"1px solid rgba(255,255,255,0.04)",padding:"10px 32px",display:"flex",alignItems:"center",gap:12,flexWrap:"wrap",background:"rgba(255,255,255,0.01)"}}>
        <span style={{fontSize:11,color:"#525252",fontWeight:600,textTransform:"uppercase",letterSpacing:"0.5px"}}>API</span>
        <input value={apiBase} onChange={e=>setApiBase(e.target.value)} placeholder="(same origin)" style={{padding:"5px 10px",borderRadius:4,border:"1px solid rgba(255,255,255,0.1)",background:"rgba(255,255,255,0.04)",color:"#e5e5e5",fontFamily:"'JetBrains Mono',monospace",fontSize:12,width:260}}/>
        <input type="password" value={apiKey} onChange={e=>setApiKey(e.target.value)} placeholder="API key (optional)" style={{padding:"5px 10px",borderRadius:4,border:"1px solid rgba(255,255,255,0.1)",background:"rgba(255,255,255,0.04)",color:"#e5e5e5",fontFamily:"'JetBrains Mono',monospace",fontSize:12,width:180}}/>
        <button onClick={checkHealth} style={{padding:"5px 12px",borderRadius:4,border:"1px solid rgba(255,255,255,0.1)",background:"rgba(255,255,255,0.04)",color:"#a3a3a3",fontSize:11,fontWeight:600,cursor:"pointer"}}>Test</button>
      </div>

      {apiError&&(<div style={{margin:"0 32px",marginTop:16,padding:"12px 16px",borderRadius:8,background:"rgba(248,113,113,0.1)",border:"1px solid rgba(248,113,113,0.25)",color:"#f87171",fontSize:13,animation:"rcl-in 0.3s ease-out"}}>{"\u26a0 "}{apiError}</div>)}

      <div style={{maxWidth:1200,margin:"0 auto",padding:32}}>
        <div style={{display:"flex",gap:12,marginBottom:32,flexWrap:"wrap"}}>
          {Object.values(SCENARIOS).map(s=>(
            <button key={s.id} onClick={()=>setActiveId(s.id)} style={{flex:"1 1 300px",padding:"16px 20px",borderRadius:10,border:activeId===s.id?"1.5px solid rgba(239,68,68,0.5)":"1px solid rgba(255,255,255,0.08)",background:activeId===s.id?"rgba(239,68,68,0.06)":"rgba(255,255,255,0.02)",color:"#e5e5e5",cursor:"pointer",textAlign:"left",transition:"all 0.2s"}}>
              <div style={{fontSize:14,fontWeight:700,marginBottom:4}}>{s.title}</div>
              <div style={{fontSize:12,color:"#737373"}}>{s.subtitle}</div>
            </button>
          ))}
        </div>

        <div style={{background:"rgba(255,255,255,0.02)",border:"1px solid rgba(255,255,255,0.06)",borderRadius:10,padding:24,marginBottom:24}}>
          <div style={{display:"flex",gap:32,flexWrap:"wrap"}}>
            <div style={{flex:"2 1 400px"}}>
              <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1px",color:"#737373",fontWeight:600,marginBottom:8}}>What happened</div>
              <div style={{fontSize:14,lineHeight:1.7,color:"#a3a3a3"}}>{sc.description}</div>
            </div>
            <div style={{flex:"1 1 200px",display:"flex",flexDirection:"column",gap:12}}>
              <div>
                <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1px",color:"#737373",fontWeight:600}}>Real blast radius</div>
                <div style={{fontSize:18,fontWeight:700,color:"#f87171",fontFamily:"'JetBrains Mono',monospace"}}>{sc.blastRadiusReal}</div>
              </div>
              <div>
                <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1px",color:"#737373",fontWeight:600}}>Time to detect</div>
                <div style={{fontSize:14,fontWeight:600,color:"#fbbf24"}}>{sc.timeToDetect}</div>
              </div>
            </div>
          </div>
        </div>

        <div style={{marginBottom:24}}>
          <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1px",color:"#737373",fontWeight:600,marginBottom:12}}>RCL Rules (Shadow Mode)</div>
          <div style={{display:"flex",gap:12,flexWrap:"wrap"}}>
            {sc.rules.map(r=>(
              <div key={r.id} style={{flex:"1 1 280px",padding:"14px 18px",borderRadius:8,border:"1px solid rgba(139,92,246,0.15)",background:"rgba(139,92,246,0.04)"}}>
                <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:6}}>
                  <span style={{fontSize:14}}>{TI[r.type]}</span>
                  <span style={{fontSize:12,fontWeight:700,color:"#c4b5fd",fontFamily:"'JetBrains Mono',monospace"}}>{r.id}</span>
                  <span style={{fontSize:13,fontWeight:600}}>{r.name}</span>
                </div>
                <div style={{fontSize:12,color:"#737373"}}>{r.description}</div>
              </div>
            ))}
          </div>
        </div>

        <div style={{display:"flex",alignItems:"center",gap:16,marginBottom:20}}>
          <button onClick={handlePlay} disabled={apiStatus!=="ok"&&!playing} style={{padding:"10px 28px",borderRadius:8,border:"none",background:playing?"rgba(234,179,8,0.15)":apiStatus==="ok"?"rgba(34,197,94,0.15)":"rgba(255,255,255,0.04)",color:playing?"#facc15":apiStatus==="ok"?"#4ade80":"#525252",fontSize:13,fontWeight:700,cursor:apiStatus==="ok"||playing?"pointer":"not-allowed",fontFamily:"'JetBrains Mono',monospace",letterSpacing:"0.5px",transition:"all 0.2s",opacity:apiStatus==="ok"||playing?1:0.5}}>{apiStatus!=="ok"&&!playing?"\u26a0 API OFFLINE":btnLabel}</button>
          <button onClick={reset} style={{padding:"10px 20px",borderRadius:8,border:"1px solid rgba(255,255,255,0.1)",background:"transparent",color:"#737373",fontSize:13,fontWeight:600,cursor:"pointer"}}>Reset</button>
          <div style={{flex:1}}/>
          <div style={{fontSize:12,color:"#525252",fontFamily:"'JetBrains Mono',monospace"}}>{safe.length} / {sc.events.length} events</div>
        </div>

        <div style={{display:"flex",gap:12,marginBottom:20,flexWrap:"wrap"}}>
          <Stat label="Allow" value={counts.allow} color="#4ade80"/>
          <Stat label="Hold for review" value={counts["hold-for-review"]} color="#facc15"/>
          <Stat label="Block" value={counts.block} color="#f87171" sub={blocked$>0?"$"+blocked$.toLocaleString()+" exposure prevented":undefined}/>
        </div>

        <div ref={logBox} style={{background:"rgba(255,255,255,0.01)",border:"1px solid rgba(255,255,255,0.06)",borderRadius:10,overflow:"hidden",maxHeight:440,overflowY:"auto"}}>
          <div style={{display:"grid",gridTemplateColumns:gridCols,gap:12,padding:"12px 20px",borderBottom:"1px solid rgba(255,255,255,0.06)",position:"sticky",top:0,background:"#0a0a0a",zIndex:2}}>
            {["Time","Entity","Amount","Verdict","Rule","Reason"].map(h=><div key={h} style={{fontSize:10,textTransform:"uppercase",letterSpacing:"1px",color:"#525252",fontWeight:700}}>{h}</div>)}
          </div>
          {safe.length===0&&<div style={{padding:"60px 20px",textAlign:"center",color:"#404040",fontSize:13}}>{apiStatus==="ok"?"Press RUN SIMULATION to evaluate events through live RCL API":"Connect to API first"}</div>}
          {safe.map((e,i)=>{
            const vs=VS[e.verdict]||FB;const last=i===safe.length-1;
            return(
              <div key={e.id+"_"+i} onClick={()=>setPicked(picked===e.id?null:e.id)} style={{display:"grid",gridTemplateColumns:gridCols,gap:12,padding:"10px 20px",borderBottom:"1px solid rgba(255,255,255,0.03)",background:picked===e.id?"rgba(255,255,255,0.04)":last?vs.bg:"transparent",borderLeft:"3px solid "+vs.border,cursor:"pointer",transition:"background 0.3s",animation:last?"rcl-in 0.3s ease-out":"none"}}>
                <div style={{fontSize:12,fontFamily:"'JetBrains Mono',monospace",color:"#737373"}}>{e.ts}</div>
                <div style={{fontSize:12,fontFamily:"'JetBrains Mono',monospace",color:"#a3a3a3",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{e.entity}</div>
                <div style={{fontSize:12,fontFamily:"'JetBrains Mono',monospace",color:"#e5e5e5",fontWeight:600}}>{fmtAmt(e.amount)}</div>
                <div><Badge verdict={e.verdict}/></div>
                <div><RuleChip ruleId={e.rule} rules={sc.rules}/></div>
                <div style={{fontSize:12,color:"#737373",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{e.note}</div>
              </div>
            );
          })}
        </div>

        {done&&(
          <div style={{marginTop:24,background:"linear-gradient(135deg,rgba(34,197,94,0.04) 0%,rgba(239,68,68,0.04) 100%)",border:"1px solid rgba(34,197,94,0.15)",borderRadius:10,padding:28,animation:"rcl-in 0.5s ease-out"}}>
            <div style={{fontSize:11,textTransform:"uppercase",letterSpacing:"1.5px",color:"#4ade80",fontWeight:700,marginBottom:20}}>Shadow Mode Summary \u2014 Live API Results</div>
            <div style={{display:"flex",gap:32,flexWrap:"wrap"}}>
              <div style={{flex:"1 1 250px"}}>
                <div style={{fontSize:11,color:"#737373",textTransform:"uppercase",letterSpacing:"0.5px",marginBottom:4}}>Exposure flagged / blocked</div>
                <div style={{fontSize:18,fontWeight:700,color:"#4ade80",fontFamily:"'JetBrains Mono',monospace"}}>{summaryPrevented}</div>
              </div>
              <div style={{flex:"1 1 200px"}}>
                <div style={{fontSize:11,color:"#737373",textTransform:"uppercase",letterSpacing:"0.5px",marginBottom:4}}>Holds / Blocks</div>
                <div style={{fontSize:18,fontWeight:700,color:"#facc15",fontFamily:"'JetBrains Mono',monospace"}}>{holdCount} holds \u00b7 {blockCount} blocks</div>
              </div>
              <div style={{flex:"1 1 200px"}}>
                <div style={{fontSize:11,color:"#737373",textTransform:"uppercase",letterSpacing:"0.5px",marginBottom:4}}>Events evaluated</div>
                <div style={{fontSize:18,fontWeight:700,color:"#a3a3a3",fontFamily:"'JetBrains Mono',monospace"}}>{safe.length} of {sc.events.length}</div>
              </div>
            </div>
            <div style={{marginTop:24,padding:"16px 20px",borderRadius:8,background:"rgba(255,255,255,0.03)",border:"1px solid rgba(255,255,255,0.06)"}}>
              <div style={{fontSize:13,color:"#a3a3a3",lineHeight:1.7}}>
                <strong style={{color:"#e5e5e5"}}>These verdicts came from the live API</strong> \u2014 not hardcoded data. The rule engine evaluated each event against the current policy (GET /v1/policy to inspect, PUT /v1/policy to change thresholds). Change the policy and replay to see different outcomes.
              </div>
            </div>
          </div>
        )}

        <div style={{marginTop:40,paddingTop:20,borderTop:"1px solid rgba(255,255,255,0.04)",textAlign:"center"}}>
          <div style={{fontSize:12,color:"#404040",lineHeight:1.8}}>
            All data is synthetic, reconstructed from public postmortems and incident reports.<br/>
            No customer data is used. Verdicts come from the live RCL API \u2014 not precomputed.
          </div>
        </div>
      </div>
    </div>
  );
}

ReactDOM.render(<App/>,document.getElementById("root"));
</script>
</body></html>"""



# ── Route handlers for HTML pages ──

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing():
    """Landing page — project overview + navigation."""
    return LANDING_HTML


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo():
    """Interactive scenario demo (React) — runs against live API."""
    return DEMO_HTML


@app.get("/log", response_class=HTMLResponse)
async def log_viewer():
    """Decision log + policy editor + rules viewer — auto-refreshes."""
    return LOG_VIEWER_HTML
