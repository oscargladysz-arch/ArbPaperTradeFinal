# Copy to config.py and fill in. config.py is gitignored -- never commit keys.

ANTHROPIC_API_KEY = "sk-ant-..."

KALSHI_API_KEY_ID = "your-kalshi-key-id"
KALSHI_PRIVATE_KEY_PATH = "keys/kalshi_private_key.pem"

KALSHI_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

CONFIRMED_MATCHES_PATH = "data/confirmed_matches.csv"
SUBSCRIPTION_MANIFEST_PATH = "data/subscription_manifest.csv"
PAPER_LEDGER_PATH = "data/paper_trades_ledger.csv"

TARGET_LIQUIDITY_USD = 50   # notional per leg per trade
COOLDOWN_SECONDS = 60       # per-pair re-fire cooldown
