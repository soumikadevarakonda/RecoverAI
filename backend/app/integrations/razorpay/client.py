import httpx
from app.core.config import settings


class RazorpayClient:
    def __init__(self, key_id: str = None, key_secret: str = None):
        self.key_id = key_id or settings.razorpay_key_id
        self.key_secret = key_secret or settings.razorpay_key_secret
        self.base_url = "https://api.razorpay.com/v1"

    def create_payment_link(
        self,
        amount: int,
        reference_id: str,
        description: str,
        expire_by: int | None = None,
    ) -> dict:
        url = f"{self.base_url}/payment_links"
        payload = {
            "amount": amount,
            "currency": "INR",
            "accept_partial": False,
            "reference_id": reference_id,
            "description": description,
            "notes": {
                "recovery_id": reference_id
            }
        }
        if expire_by:
            payload["expire_by"] = expire_by

        response = httpx.post(
            url,
            json=payload,
            auth=(self.key_id, self.key_secret),
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()
