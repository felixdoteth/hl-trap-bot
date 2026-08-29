"""
Deriv WebSocket client — Step 1, rewritten for the current API
(the old direct wss://ws.derivws.com/websockets/v3?app_id=X + in-band
{"authorize": token} pattern is deprecated — this now goes through the
REST accounts -> OTP -> scoped-WebSocket-URL flow Deriv actually uses).
"""
from __future__ import annotations

import asyncio
import json
import os
import requests
import websockets

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "")
DERIV_TOKEN  = os.getenv("DERIV_TOKEN", "")
REST_BASE    = "https://api.derivws.com/trading/v1/options"


class DerivClient:
    def __init__(self, token: str = DERIV_TOKEN, app_id: str = DERIV_APP_ID) -> None:
        if not token or not app_id:
            raise RuntimeError("Set DERIV_APP_ID and DERIV_TOKEN env vars first")
        self.token = token
        self.app_id = app_id
        self.headers = {
            "Deriv-App-ID": self.app_id,
            "Authorization": f"Bearer {self.token}",
        }
        self.ws = None
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def get_accounts(self) -> list[dict]:
        """REST call — returns your actual account list, no OTP needed yet."""
        r = requests.get(f"{REST_BASE}/accounts", headers=self.headers, timeout=10)
        r.raise_for_status()
        accounts = r.json()["data"]
        for a in accounts:
            print(f"  {a['account_id']} | {a['account_type']} | "
                  f"{a['balance']} {a['currency']} | {a['status']}")
        return accounts

    def get_ws_url(self, account_id: str) -> str:
        """POST for a fresh OTP — the response URL already has it embedded.
        OTPs are short-lived, so this has to happen right before connecting,
        not be cached and reused."""
        r = requests.post(f"{REST_BASE}/accounts/{account_id}/otp",
                           headers=self.headers, timeout=10)
        r.raise_for_status()
        return r.json()["data"]["url"]

    async def connect(self, ws_url: str) -> None:
        self.ws = await websockets.connect(ws_url, ping_interval=20, ping_timeout=20)
        print(f"Connected: {ws_url.split('?')[0]}?otp=***")

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None
            print("Disconnected")

    async def send(self, payload: dict) -> dict:
        if not self.ws:
            raise RuntimeError("Not connected — call connect() first")
        payload = {**payload, "req_id": self._next_id()}
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get("req_id") == payload["req_id"]:
                if data.get("error"):
                    err = data["error"]
                    raise RuntimeError(f"Deriv API error {err.get('code')}: {err.get('message')}")
                return data

    async def balance(self) -> float:
        data = await self.send({"balance": 1})
        bal = float(data["balance"]["balance"])
        print(f"Live balance via WS: {bal}")
        return bal


async def main() -> None:
    client = DerivClient()

    print("Fetching accounts...")
    accounts = client.get_accounts()
    demo = next((a for a in accounts if a["account_type"] == "demo"), None)
    if not demo:
        print("No demo account found on this token.")
        return

    print(f"\nRequesting OTP for {demo['account_id']}...")
    ws_url = client.get_ws_url(demo["account_id"])

    try:
        await client.connect(ws_url)
        await client.balance()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())