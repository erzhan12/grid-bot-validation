#!/usr/bin/env python3
"""
Simple Position Calculator Script

Allows users to quickly test position calculations by inputting their own parameters.
Usage: python test_my_position.py
"""

import os
import sys

sys.path.append(os.getcwd())

from src.enums import Direction
from tests.test_position_field_calculations import calculate_user_scenario


def get_user_input():
    """Get position parameters from user input"""
    print("🎯 Position Calculator")
    print("=" * 40)

    # Get basic parameters
    position_size = float(input("Enter position size (e.g., 0.1): "))
    entry_price = float(input("Enter entry price (e.g., 50000): "))
    current_price = float(input("Enter current price (e.g., 55000): "))
    total_balance = float(input("Enter total balance (e.g., 10000): "))

    # Get direction
    direction_input = input("Enter direction (long/short) [default: long]: ").strip().lower()
    if direction_input == "short":
        direction = Direction.SHORT
    else:
        direction = Direction.LONG

    # Get leverage
    leverage_input = input("Enter leverage (e.g., 10) [default: 10]: ").strip()
    leverage = int(leverage_input) if leverage_input else 10

    # Get symbol
    symbol = input("Enter symbol (e.g., BTCUSDT) [default: BTCUSDT]: ").strip()
    if not symbol:
        symbol = "BTCUSDT"

    return position_size, entry_price, current_price, total_balance, direction, leverage, symbol


def main():
    """Main function to run position calculator"""
    try:
        # Get user inputs
        position_size, entry_price, current_price, total_balance, direction, leverage, symbol = get_user_input()

        # Run the calculation
        result = calculate_user_scenario(
            position_size=position_size,
            entry_price=entry_price,
            current_last_close=current_price,
            total_balance=total_balance,
            direction=direction,
            leverage=leverage,
            symbol=symbol
        )

        # Show risk assessment
        summary = result['summary']
        print("\n🎯 Risk Assessment:")
        print(f"   Position Status: {'✅ Healthy' if summary['position_healthy'] else '⚠️  At Risk'}")
        print(f"   P&L Status: {summary['profit_loss']}")
        print(f"   Margin Utilization: {summary['margin_utilization']:.2f}%")

        if summary['margin_utilization'] > 50:
            print("   ⚠️  WARNING: High margin utilization - consider reducing position size")

        if not summary['position_healthy']:
            print("   🚨 ALERT: Position is close to liquidation!")

    except KeyboardInterrupt:
        print("\n\n👋 Thanks for using the position calculator!")
    except ValueError as e:
        print(f"\n❌ Error: Please enter valid numbers. {e}")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")


if __name__ == "__main__":
    main()