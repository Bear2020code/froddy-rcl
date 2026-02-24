/**
 * Froddy RCL — Node.js Integration Example
 *
 * Minimal client for integrating with Froddy RCL shadow-mode API.
 * Zero dependencies (uses built-in fetch, Node 18+).
 */

class RCLClient {
  constructor(baseUrl, apiKey, timeoutMs = 3000) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.apiKey = apiKey;
    this.timeoutMs = timeoutMs;
  }

  /**
   * Evaluate a payout event. Returns verdict: allow | hold-for-review | block.
   * Fail-open: if RCL is unreachable, returns {verdict: "allow", fallback: true}.
   */
  async evaluate({ eventId, entityId, amount, eventType = "payout", currency = "USD", scenario = "v1" }) {
    const payload = {
      event_id: eventId,
      entity_id: entityId,
      amount,
      event_type: eventType,
      currency,
      scenario,
      timestamp: new Date().toISOString(),
    };

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

      const resp = await fetch(`${this.baseUrl}/v1/evaluate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-API-Key": this.apiKey },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      clearTimeout(timeout);

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.json();
    } catch (err) {
      // Fail-open: RCL down = allow
      console.warn(`[RCL] Fail-open: ${err.message}`);
      return { verdict: "allow", fallback: true, error: err.message };
    }
  }

  async health() {
    const resp = await fetch(`${this.baseUrl}/health`);
    return resp.json();
  }
}

// ── Usage ──

async function main() {
  const rcl = new RCLClient("https://froddy.net", "rcl_YOUR_KEY_HERE");

  // Health check
  const health = await rcl.health();
  console.log(`RCL status: ${health.status}, db: ${health.db_healthy}`);

  // Evaluate a payout
  const result = await rcl.evaluate({
    eventId: "payout_20260224_001",
    entityId: "partner_abc123", // Pseudonymous token — you generate this
    amount: 15000.0,
    eventType: "payout",
    currency: "USD",
  });

  console.log(`Verdict: ${result.verdict}`);
  if (result.verdict !== "allow") {
    console.log(`  Rule: ${result.rule_id} — ${result.reason}`);
  }

  // Your payout logic continues regardless (shadow mode)
  // await processPayout(...)
}

main().catch(console.error);

module.exports = { RCLClient };
