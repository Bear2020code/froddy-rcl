"""
Froddy RCL — Python Integration Example

Minimal async client for integrating with Froddy RCL shadow-mode API.
Drop this into your payout service to start sending events.

Requirements: pip install httpx
"""

import httpx
import asyncio
from datetime import datetime, timezone


class RCLClient:
    """Lightweight async client for Froddy RCL API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    async def evaluate(
        self,
        event_id: str,
        entity_id: str,
        amount: float,
        event_type: str = "payout",
        currency: str = "USD",
        scenario: str = "v1",
        timestamp: str | None = None,
    ) -> dict:
        """
        Evaluate a payout event. Returns verdict: allow | hold-for-review | block.

        Fail-open: if RCL is unreachable, returns {"verdict": "allow", "fallback": True}
        so your payout process is never blocked.
        """
        payload = {
            "event_id": event_id,
            "entity_id": entity_id,
            "amount": amount,
            "event_type": event_type,
            "currency": currency,
            "scenario": scenario,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = await self._client.post("/v1/evaluate", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            # Fail-open: RCL down = allow
            print(f"[RCL] Fail-open: {e}")
            return {"verdict": "allow", "fallback": True, "error": str(e)}

    async def health(self) -> dict:
        """Check RCL health."""
        resp = await self._client.get("/health")
        return resp.json()

    async def export_csv(self, date_from: str | None = None, date_to: str | None = None) -> str:
        """Export decision log as CSV."""
        params = {"format": "csv"}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        resp = await self._client.get("/v1/decisions/export", params=params)
        return resp.text

    async def close(self):
        await self._client.aclose()


# ── Usage example ──

async def main():
    # Replace with your API key (from tenant creation)
    rcl = RCLClient(
        base_url="https://froddy.net",
        api_key="rcl_YOUR_KEY_HERE",
    )

    # Check health
    health = await rcl.health()
    print(f"RCL status: {health['status']}, db: {health['db_healthy']}")

    # Evaluate a payout event
    result = await rcl.evaluate(
        event_id="payout_20260224_001",
        entity_id="partner_abc123",  # Pseudonymous token — you generate this
        amount=15000.00,
        event_type="payout",
        currency="USD",
    )

    print(f"Verdict: {result['verdict']}")
    if result["verdict"] != "allow":
        print(f"  Rule: {result.get('rule_id')} — {result.get('reason')}")

    # Your payout logic continues regardless (shadow mode)
    # process_payout(...)

    await rcl.close()


if __name__ == "__main__":
    asyncio.run(main())
