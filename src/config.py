"""
Configuration constants for the EMA pullback Polymarket bot
"""

import os
from dotenv import load_dotenv

load_dotenv()

EMA_PERIOD            = 8
NO_TOUCH_BARS         = 1
TREND_CHECK_BARS      = 5
MAX_TOUCHES_FOR_RANGE = 3

MIN_ENTRY_PRICE = 0.30
MAX_ENTRY_PRICE = 0.55
TRADE_SIZE_USDC = float(os.getenv("TRADE_SIZE_USDC", "2.0"))

GAMMA_API     = "https://gamma-api.polymarket.com"
TAG_CRYPTO    = "crypto"

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

POLL_INTERVAL_SEC = 15
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
