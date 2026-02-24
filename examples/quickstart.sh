#!/bin/bash
# Froddy RCL — Quickstart (curl)
# Replace rcl_YOUR_KEY_HERE with your tenant API key.

BASE="https://froddy.net"
KEY="rcl_YOUR_KEY_HERE"

echo "=== 1. Health check ==="
curl -s "$BASE/health" | python3 -m json.tool

echo ""
echo "=== 2. Evaluate a payout event ==="
curl -s -X POST "$BASE/v1/evaluate" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{
    "event_id": "test_001",
    "entity_id": "partner_alpha",
    "amount": 15000,
    "event_type": "payout",
    "currency": "USD",
    "scenario": "v1"
  }' | python3 -m json.tool

echo ""
echo "=== 3. View current policy ==="
curl -s "$BASE/v1/policy" -H "X-API-Key: $KEY" | python3 -m json.tool

echo ""
echo "=== 4. View active rules ==="
curl -s "$BASE/v1/rules" -H "X-API-Key: $KEY" | python3 -m json.tool

echo ""
echo "=== 5. Query decision log ==="
curl -s "$BASE/v1/decisions?limit=5" | python3 -m json.tool

echo ""
echo "=== 6. Export decisions (CSV) ==="
curl -s "$BASE/v1/decisions/export?format=csv" -H "X-API-Key: $KEY"

echo ""
echo "=== 7. Configure webhook (optional) ==="
echo "# Set webhook URL for hold/block alerts:"
echo "# curl -s -X PUT $BASE/v1/webhook -H 'X-API-Key: $KEY' -H 'Content-Type: application/json' -d '{\"url\": \"https://hooks.slack.com/services/YOUR/HOOK/URL\"}'"

echo ""
echo "=== 8. Pilot report ==="
curl -s "$BASE/v1/report" -H "X-API-Key: $KEY" | python3 -m json.tool
