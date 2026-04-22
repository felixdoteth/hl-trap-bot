"""
CoreWriter SDK for HyperEVM
The only Python library for calling HyperCore L1 actions from EVM.

Author: @felixdoteth
Contract: 0x3333333333333333333333333333333333333333
"""

from web3 import Web3
from eth_abi import encode
from eth_account import Account
from typing import Optional
import requests

COREWRITER = "0x3333333333333333333333333333333333333333"
SEND_RAW_ACTION = bytes.fromhex("17938e13")

# Action IDs
ACTION_USD_TRANSFER    = 0x000007  # Transfer USD between EVM and Perp
ACTION_SPOT_ORDER      = 0x000002  # Place spot order
ACTION_CANCEL_ORDER    = 0x000003  # Cancel order
ACTION_PERP_ORDER      = 0x000001  # Place perp order

class CoreWriter:
    def __init__(self, private_key: str, rpc: str = "https://rpc.hyperliquid.xyz/evm"):
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.account = Account.from_key(private_key)
        self.address = self.account.address

    def _send(self, action_id: int, payload: bytes, gas: int = 150000) -> str:
        """Send a raw CoreWriter action to HyperCore L1."""
        action_data = bytes([0x01]) + action_id.to_bytes(3, 'big') + payload
        calldata = SEND_RAW_ACTION + encode(['bytes'], [action_data])

        tx = {
            'to': COREWRITER,
            'data': calldata,
            'gas': gas,
            'gasPrice': self.w3.to_wei('0.1', 'gwei'),
            'chainId': 999,
            'nonce': self.w3.eth.get_transaction_count(self.address),
            'value': 0,
        }
        signed  = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        return tx_hash.hex(), receipt.status

    def transfer_to_perp(self, amount_usd: float) -> tuple:
        """
        Transfer USD from EVM wallet to Perp account.
        amount_usd: amount in USD (e.g. 100.0 = $100)
        """
        ntl = int(amount_usd * 1e6)
        payload = encode(['uint64', 'bool'], [ntl, True])
        return self._send(ACTION_USD_TRANSFER, payload)

    def transfer_to_evm(self, amount_usd: float) -> tuple:
        """
        Transfer USD from Perp account to EVM wallet.
        amount_usd: amount in USD
        """
        ntl = int(amount_usd * 1e6)
        payload = encode(['uint64', 'bool'], [ntl, False])
        return self._send(ACTION_USD_TRANSFER, payload)

    def place_spot_order(
        self,
        asset: int,
        is_buy: bool,
        limit_price: float,
        size: float,
        reduce_only: bool = False,
        tif: int = 2,  # 1=ALO, 2=GTC, 3=IOC
        cloid: int = 0
    ) -> tuple:
        """
        Place a spot order on HyperCore.
        asset: spot asset index (e.g. 10000 for BTC spot)
        limit_price: price in USD
        size: size in base asset units
        tif: 1=ALO, 2=GTC, 3=IOC
        """
        limit_px = int(limit_price * 1e8)
        sz       = int(size * 1e8)
        payload  = encode(
            ['uint32', 'bool', 'uint64', 'uint64', 'bool', 'uint8', 'uint64'],
            [asset, is_buy, limit_px, sz, reduce_only, tif, cloid]
        )
        return self._send(ACTION_SPOT_ORDER, payload)

    def place_perp_order(
        self,
        asset: int,
        is_buy: bool,
        limit_price: float,
        size: float,
        reduce_only: bool = False,
        tif: int = 2,
        cloid: int = 0
    ) -> tuple:
        """
        Place a perp order on HyperCore.
        asset: perp asset index (e.g. 0 for BTC-PERP)
        """
        limit_px = int(limit_price * 1e8)
        sz       = int(size * 1e8)
        payload  = encode(
            ['uint32', 'bool', 'uint64', 'uint64', 'bool', 'uint8', 'uint64'],
            [asset, is_buy, limit_px, sz, reduce_only, tif, cloid]
        )
        return self._send(ACTION_PERP_ORDER, payload)

    def cancel_all(self, asset: int = 0) -> tuple:
        """Cancel all orders for an asset."""
        payload = encode(['uint32'], [asset])
        return self._send(ACTION_CANCEL_ORDER, payload)

    def get_perp_balance(self) -> dict:
        """Get perp account balance from HyperCore API."""
        r = requests.post('https://api.hyperliquid.xyz/info',
            json={'type': 'clearinghouseState', 'user': self.address}, timeout=5)
        state = r.json()
        margin = state.get('marginSummary', {})
        return {
            'account_value': float(margin.get('accountValue', 0)),
            'withdrawable':  float(margin.get('withdrawable', 0)),
            'margin_used':   float(margin.get('totalMarginUsed', 0)),
        }

    def get_oracle_price(self, asset_index: int) -> float:
        """
        Get oracle price for asset from HyperEVM precompile 0x807.
        Returns price in USD. Use asset_index=0 for BTC, 1 for ETH, 17 for HYPE.
        """
        data = '0x' + asset_index.to_bytes(32, 'big').hex()
        result = self.w3.eth.call({
            'to': '0x0000000000000000000000000000000000000807',
            'data': data
        })
        raw = int(result.hex(), 16)
        # BTC (idx 0) needs /10, ETH (idx 1) needs /100, HYPE (idx 17) needs /1000
        divisors = {0: 10, 1: 100, 17: 1000}
        return raw / divisors.get(asset_index, 1)

    def get_spot_balance(self, token_index: int) -> int:
        """Get spot balance from HyperEVM precompile 0x801."""
        data = Web3.to_bytes(hexstr=
            '0x' +
            '000000000000000000000000' + self.address[2:].lower() +
            token_index.to_bytes(32, 'big').hex()
        )
        result = self.w3.eth.call({
            'to': '0x0000000000000000000000000000000000000801',
            'data': data
        })
        return int.from_bytes(result[:8], 'big') if result else 0


# ── Quick demo ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import os
    PK = os.environ.get('DEPLOYER_PK')
    if not PK:
        print('Set DEPLOYER_PK env var to run demo')
        exit()

    cw = CoreWriter(PK)
    print(f'Address: {cw.address}')
    print()

    print('Oracle prices:')
    print(f'  BTC  (idx 0):  \${cw.get_oracle_price(0):,.2f}')
    print(f'  ETH  (idx 1):  \${cw.get_oracle_price(1):,.2f}')
    print(f'  HYPE (idx 17): \${cw.get_oracle_price(17):,.4f}')
    print()

    print('Perp balance:')
    bal = cw.get_perp_balance()
    for k,v in bal.items():
        print(f'  {k}: \${v:,.4f}')
    print()

    print('Testing cancel_all (safe, no-op if no orders)...')
    tx, status = cw.cancel_all()
    print(f'  tx={tx} status={status}')

    def set_leverage(self, asset: int, leverage: int, is_cross: bool = False) -> dict:
        """Set leverage for a perp asset via Hyperliquid L1 API."""
        import json, time
        from eth_account.messages import encode_defunct
        action = {
            "type": "updateLeverage",
            "asset": asset,
            "isCross": is_cross,
            "leverage": leverage
        }
        nonce = int(time.time() * 1000)
        msg = json.dumps({"action": action, "nonce": nonce}, separators=(',', ':'))
        signed = self.account.sign_message(encode_defunct(text=msg))
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}
        }
        r = requests.post('https://api.hyperliquid.xyz/exchange', json=payload, timeout=5)
        return r.json()
