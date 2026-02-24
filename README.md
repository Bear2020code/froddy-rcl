# Froddy RCL

**Financial circuit breaker for automated operations.**

RCL is an external rule-based control layer that evaluates every payout/disbursement in real time and assigns a verdict: `allow`, `hold-for-review`, or `block`. Shadow mode first, enforcement when you're ready.

🌐 **[froddy.net](https://froddy.net)** · 🎮 **[Live Demo](https://froddy.net/demo)** · 📄 **[API Docs](https://froddy.net/docs)**

---

## Quick Start

### 1. Get your API key

Contact [hello@froddy.net](mailto:hello@froddy.net) or [Telegram @froddynet](https://t.me/froddynet) to set up a pilot tenant.

### 2. Send your first event

```bash
curl -X POST https://froddy.net/v1/evaluate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: rcl_YOUR_KEY" \
  -d '{
    "event_id": "payout_001",
    "entity_id": "partner_abc123",
    "amount": 15000,
    "event_type": "payout",
    "scenario": "v1"
  }'
```

Response:
```json
{
  "event_id": "payout_001",
  "verdict": "allow",
  "rule_id": null,
  "reason": "All rules passed",
  "evaluated_at": "2026-02-24T12:00:00.000000"
}
```

### 3. Integrate (Python)

```python
from rcl_client import RCLClient

rcl = RCLClient("https://froddy.net", "rcl_YOUR_KEY")
result = await rcl.evaluate(
    event_id="payout_001",
    entity_id="partner_abc123",  # pseudonymous token you generate
    amount=15000.00,
)
# result["verdict"] → "allow" | "hold-for-review" | "block"
# Your payout continues regardless (shadow mode)
```

See also: [Node.js example](examples/node_client.js)

---

## How It Works

```
Your Payout Service ──async POST──▶ Froddy RCL ──▶ Verdict + Log
        │                                │
        │  (continues regardless)        │ hold/block? ──▶ Webhook (Slack)
        ▼                                ▼
   Process payout                   Decision Log (CSV/JSON)
```

- **Shadow mode**: RCL observes and labels — does NOT block operations
- **Fail-open**: If RCL is unreachable, your process continues normally
- **Kill-switch**: Feature flag on your side — disable in minutes

---

## Rules (MVP)

| Rule | What it does |
|------|-------------|
| **R-CEIL** | Daily exposure ceiling per entity (e.g., $50K/day) |
| **R-VEL** | Velocity spike — too many transactions in a time window |
| **R-COHORT** | Single-transaction anomaly — amount exceeds thresholds |

Thresholds are configurable per tenant via `PUT /v1/policy`.

---

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/v1/evaluate` | POST | API key | Evaluate event, return verdict |
| `/v1/decisions` | GET | — | Query decision log |
| `/v1/decisions/export` | GET | API key | Export log (CSV/JSON) |
| `/v1/policy` | GET/PUT | API key | Policy thresholds |
| `/v1/rules` | GET | API key | Active rules |
| `/v1/webhook` | GET/PUT/DELETE | API key | Alert webhook (Slack, etc.) |
| `/v1/report` | GET | API key | Pilot report data |
| `/v1/sensitivity` | POST | API key | What-if threshold analysis |
| `/health` | GET | — | Health + fail-open status |

Full Swagger UI: **[froddy.net/docs](https://froddy.net/docs)**

---

## Payload

```json
{
  "event_id": "unique_idempotent_key",
  "entity_id": "pseudonymous_token",
  "amount": 15000.00,
  "event_type": "payout",
  "currency": "USD",
  "scenario": "v1"
}
```

**We do NOT accept**: names, emails, banking details, IP addresses. Pseudonymization is on your side.

---

## Pilot Process

| Week | Phase | Description |
|------|-------|-------------|
| 0 | Calibration | Threshold tuning, test calls, data mapping |
| 1–4 | Observation | Shadow mode, weekly review (≤2 hrs/week) |
| 5 | Report | Final report + recommendation |

**Cost**: Pilot is free. No purchase obligation.

---

## Examples

- [`examples/python_client.py`](examples/python_client.py) — Async Python client with fail-open
- [`examples/node_client.js`](examples/node_client.js) — Node.js client (zero deps)
- [`examples/quickstart.sh`](examples/quickstart.sh) — curl quickstart

---

## Contact

📧 [hello@froddy.net](mailto:hello@froddy.net) · 💬 [Telegram @froddynet](https://t.me/froddynet) · 🌐 [froddy.net](https://froddy.net)
