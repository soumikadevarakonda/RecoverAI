import hashlib
import hmac
import json

import httpx


SECRET = "test-webhook-secret"

payload = {
    "entity": "event",
    "event": "payment.failed",
    "payload": {},
}

body = json.dumps(payload, separators=(",", ":")).encode()

signature = hmac.new(
    SECRET.encode(),
    body,
    hashlib.sha256,
).hexdigest()

headers = {
    "x-razorpay-signature": signature,
    "x-razorpay-event-id": "evt_test_001",
    "Content-Type": "application/json",
}

response = httpx.post(
    "http://127.0.0.1:8000/api/v1/webhooks/razorpay",
    content=body,
    headers=headers,
)

print("Status:", response.status_code)
print("Response:", response.json())