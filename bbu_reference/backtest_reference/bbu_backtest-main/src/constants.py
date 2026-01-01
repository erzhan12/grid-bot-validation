"""
Constants used throughout the backtest system.
"""

# Commission rates (Bybit USDT Perpetual)
COMMISSION_RATE = 0.0002  # 0.02% maker fee for limit orders in backtesting (2 bps)
TAKER_FEE_RATE = 0.0006  # 0.06% taker fee
MAKER_FEE_RATE = 0.0001  # 0.01% maker fee

# Position management
DEFAULT_LEVERAGE = 10
MAINTENANCE_MARGIN_RATE = 0.01  # 1% MMR for most pairs (will be replaced by tiers)

# Funding
FUNDING_INTERVAL_HOURS = 8  # Funding payments every 8 hours
DEFAULT_FUNDING_RATE = 0.0001  # Default 0.01% funding rate

# Order management
MIN_AMOUNT_USDT = 5  # Minimum order value in USDT

# Maintenance margin tiers for BTCUSDT (Bybit official tiers)
MM_TIERS_BTCUSDT = [
    {"min": 0, "max": 2000000, "mmr": 0.005, "deduction": 0},
    {"min": 2000000, "max": 10000000, "mmr": 0.01, "deduction": 10000},
    {"min": 10000000, "max": 20000000, "mmr": 0.025, "deduction": 160000},
    {"min": 20000000, "max": 40000000, "mmr": 0.05, "deduction": 660000},
    {"min": 40000000, "max": 80000000, "mmr": 0.1, "deduction": 2660000},
    {"min": 80000000, "max": 160000000, "mmr": 0.125, "deduction": 4660000},
    {"min": 160000000, "max": float('inf'), "mmr": 0.15, "deduction": 8660000}
]

# Maintenance margin tiers for ETHUSDT
MM_TIERS_ETHUSDT = [
    {"min": 0, "max": 1000000, "mmr": 0.005, "deduction": 0},
    {"min": 1000000, "max": 5000000, "mmr": 0.01, "deduction": 5000},
    {"min": 5000000, "max": 10000000, "mmr": 0.025, "deduction": 80000},
    {"min": 10000000, "max": 20000000, "mmr": 0.05, "deduction": 330000},
    {"min": 20000000, "max": 40000000, "mmr": 0.1, "deduction": 1330000},
    {"min": 40000000, "max": 80000000, "mmr": 0.125, "deduction": 2330000},
    {"min": 80000000, "max": float('inf'), "mmr": 0.15, "deduction": 4330000}
]

# Default maintenance margin tiers (used for unknown symbols)
MM_TIERS_DEFAULT = [
    {"min": 0, "max": 1000000, "mmr": 0.01, "deduction": 0},
    {"min": 1000000, "max": 5000000, "mmr": 0.025, "deduction": 15000},
    {"min": 5000000, "max": 10000000, "mmr": 0.05, "deduction": 140000},
    {"min": 10000000, "max": 20000000, "mmr": 0.1, "deduction": 640000},
    {"min": 20000000, "max": float('inf'), "mmr": 0.15, "deduction": 1640000}
]

# Symbol to MM tiers mapping
MM_TIERS_BY_SYMBOL = {
    'BTCUSDT': MM_TIERS_BTCUSDT,
    'ETHUSDT': MM_TIERS_ETHUSDT,
}
