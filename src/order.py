import os
from dotenv import load_dotenv
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, OrderArgs
from py_clob_client.constants import POLYGON
from web3 import Web3
from py_clob_client.order_builder.constants import BUY, SELL   # for future GTC if needed

load_dotenv()

def get_client(user_private_key=None, user_proxy_address=None, signature_type=None):
    pk = user_private_key or os.getenv("POLYGON_PRIVATE_KEY")
    proxy = user_proxy_address or os.getenv("USER_PROXY_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS")
    sig_type = int(signature_type or os.getenv("SIGNATURE_TYPE", 1))

    # Use env credentials if available, otherwise derive them
    clob_api_key = os.getenv("CLOB_API_KEY")
    clob_secret = os.getenv("CLOB_SECRET")
    clob_passphrase = os.getenv("CLOB_PASSPHRASE")
    
    if clob_api_key and clob_secret and clob_passphrase:
        from py_clob_client.clob_types import ApiCreds
        api_creds = ApiCreds(
            api_key=clob_api_key,
            api_secret=clob_secret,
            api_passphrase=clob_passphrase
        )
    else:
        temp_client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=POLYGON,
        )
        api_creds = temp_client.create_or_derive_api_creds()

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=pk,
        chain_id=POLYGON,
        creds=api_creds,
        signature_type=sig_type,
        funder=proxy,
    )
    return client


def place_order(token_id: str, size_usdc: float, user_private_key=None, user_proxy_address=None, signature_type=None):
    try:
        client = get_client(user_private_key, user_proxy_address, signature_type)
        order_args = MarketOrderArgs(token_id=token_id, amount=size_usdc, side="BUY")
        signed_order = client.create_market_order(order_args)
        try:
            resp = client.post_order(signed_order, OrderType.FOK)
        except Exception as e:
            msg = str(e).lower()
            if "fok" in msg or "fully filled" in msg or "killed" in msg:
                print("⚠️ FOK failed, retrying as FAK...")
                resp = client.post_order(signed_order, OrderType.FAK)
            else:
                raise
        proxy_short = (user_proxy_address or "")[:10]
        print(f"✅ ORDER PLACED | ${size_usdc} | proxy: {proxy_short}... | resp: {resp}")
        return resp
    except Exception as e:
        print(f"❌ Order error: {e}")
        return None


def redeem_position(condition_id: str, index_set: int = 1, user_private_key=None, user_proxy_address=None, signature_type=1, gas_price=None):
    try:
        pk = user_private_key or os.getenv("USER_PRIVATE_KEY") or os.getenv("POLYGON_PRIVATE_KEY")
        proxy = user_proxy_address or os.getenv("USER_PROXY_ADDRESS") or os.getenv("POLYMARKET_PROXY_ADDRESS")
        sig_type = int(signature_type or os.getenv("SIGNATURE_TYPE", 1))

        if not pk:
            raise ValueError("No private key provided for redeem")

        w3 = Web3(Web3.HTTPProvider(os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")))
        account = Account.from_key(pk)

        CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        USDC = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

        abi = [{
            "name": "redeemPositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"},
            ],
            "outputs": [],
        }]

        ctf = w3.eth.contract(address=CTF, abi=abi)

        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = ctf.functions.redeemPositions(
            USDC,
            b"\x00" * 32,
            bytes.fromhex(condition_id[2:]),
            [index_set],
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 250000,
            "gasPrice": gas_price or int(w3.eth.gas_price * 1.4),
        })

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        print(f"💰 REDEEM SENT | condition: {condition_id[:10]}... | index_set: {index_set} | proxy: {proxy[:10] if proxy else 'N/A'}... | tx: {tx_hash.hex()}")
        return tx_hash.hex()

    except Exception as e:
        print(f"❌ Redeem error: {e}")
        import traceback
        traceback.print_exc()
        return None


def place_gtc_order(token_id: str, price: float, shares: int, user_private_key=None, user_proxy_address=None, signature_type=1):
    try:
        client = get_client(user_private_key, user_proxy_address, signature_type)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=BUY
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        print(f"✅ GTC ORDER placed")
        return resp
    except Exception as e:
        print(f"❌ GTC error: {e}")
        return None
