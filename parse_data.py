# parse_data.py
import re
import logging

# Configure logging for the parser
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def convert_duration_text_to_seconds(duration_text: str) -> int:
    """
    Converts duration text like '5M', '1H' into seconds.
    Assumes M for Minutes, H for Hours.
    """
    duration_text = duration_text.strip().upper()
    if 'M' in duration_text:
        try:
            minutes = int(duration_text.replace('M', ''))
            return minutes * 60
        except ValueError:
            logger.warning(f"Could not parse minutes from duration text: {duration_text}")
            return 0
    elif 'H' in duration_text:
        try:
            hours = int(duration_text.replace('H', ''))
            return hours * 3600
        except ValueError:
            logger.warning(f"Could not parse hours from duration text: {duration_text}")
            return 0
    logger.warning(f"Unrecognized duration format: {duration_text}. Returning 0 seconds.")
    return 0

def parse_macrodroid_trade_data(notification_text: str) -> dict:
    """
    Parses a plain text notification string from Macrodroid into a structured trade data dictionary.
    Assumes ALL assets are for OTC trading on Pocket Option.

    Args:
        notification_text: A string containing the trade signal notification.
                           Example:
                           "🇪🇺 EUR/USD 🇺🇸 OTC\n🕘 Expiration 5M\n⏺ Entry at 10:59\n🟩 BUY\n\n🔼 Martingale levels..."

    Returns:
        A dictionary containing the parsed trade data: 'asset_name_for_po', 'duration', 'direction'.
        Returns an empty dictionary if parsing fails or essential data is missing.
    """
    parsed_data = {}
    
    # Asset (e.g., EUR/USD or GBPCHF)
    # This regex now focuses on getting the base currency pair, assuming it's always OTC.
    asset_match = re.search(r'([A-Z]{3}/[A-Z]{3}|\b[A-Z]{6}\b)\s*(OTC)?', notification_text)
    if asset_match:
        # Normalize to remove slash if present, and then append 'T' for OTC.
        # Example: EUR/USD -> EURUSD -> EURUSDT
        # Example: GBPCHF -> GBPCHF -> GBPCHFT
        base_asset = asset_match.group(1).replace('/', '').strip()
        parsed_data["asset_name_for_po"] = base_asset + "T"
        logger.info(f"Detected Asset: {asset_match.group(1)} (Assumed OTC) -> PO API Name: {parsed_data['asset_name_for_po']}")
    else:
        logger.error("Could not find asset (e.g., EUR/USD or GBPCHF) in notification text.")
        return {}

    # Expiration (e.g., 5M) and Duration in seconds
    # The constraint is duration will ALWAYS be 5 minutes (300 seconds), so we'll enforce this in trader.py.
    # We still parse it here for robustness and logging.
    expiration_match = re.search(r'Expiration\s+(\d+[MH])', notification_text)
    if expiration_match:
        parsed_data["duration_text"] = expiration_match.group(1) # Store original text for logging
        parsed_data["duration"] = convert_duration_text_to_seconds(parsed_data["duration_text"])
        if parsed_data["duration"] == 0: # Check if conversion failed
            logger.error(f"Invalid duration format found: {parsed_data['duration_text']}. Could not convert to seconds.")
            return {}
        logger.info(f"Detected Expiration: {parsed_data['duration_text']} ({parsed_data['duration']} seconds)")
    else:
        logger.error("Could not find expiration (e.g., 5M) in notification text.")
        return {}

    # Direction (e.g., BUY)
    direction_match = re.search(r'(🟩|🟥|⬆️|⬇️|🔼|🔽)\s*(BUY|SELL|UP|DOWN)', notification_text, re.IGNORECASE)
    if direction_match:
        keyword = direction_match.group(2).upper()
        if keyword in ["UP", "BUY"]:
            parsed_data["direction"] = "BUY"
        elif keyword in ["DOWN", "SELL"]:
            parsed_data["direction"] = "SELL"
        else:
            logger.error(f"Unrecognized direction keyword: {keyword}. Must be BUY/SELL/UP/DOWN.")
            return {}
        logger.info(f"Detected Direction: {parsed_data['direction']}")
    else:
        logger.error("Could not find direction (e.g., BUY/SELL/UP/DOWN) in notification text.")
        return {}
        
    # Entry Time (e.g., 10:59) - Optional, might not be used directly by API for placing trade
    entry_time_match = re.search(r'Entry at (\d{2}:\d{2})', notification_text)
    if entry_time_match:
        parsed_data["entryTime"] = entry_time_match.group(1)
        logger.info(f"Detected Entry Time: {parsed_data['entryTime']}")
    else:
        parsed_data["entryTime"] = None
        logger.warning("Could not find entry time in notification text (optional for trade execution).")

    logger.info(f"Successfully parsed raw notification into: {parsed_data}")
    return parsed_data

if __name__ == "__main__":
    # --- Test Cases ---

    # Test Case 1: Valid notification format (EUR/USD, now implicitly OTC)
    valid_notification_otc_buy = """
🇪🇺 EUR/USD 🇺🇸 OTC
🕘 Expiration 5M
⏺ Entry at 10:59
🟩 BUY

🔼 Martingale levels
1️⃣ level at 08:40
2️⃣ level at 08:45
3️⃣ level at 08:50

💥 GET THIS SIGNAL HERE!
💰 HOW TO START?
"""
    logger.info("\n--- Testing with Valid BUY Notification (Implicitly OTC) ---")
    parsed_trade_otc_buy = parse_macrodroid_trade_data(valid_notification_otc_buy)
    if parsed_trade_otc_buy:
        logger.info(f"Result: {parsed_trade_otc_buy}")
    else:
        logger.error("Failed to parse valid BUY trade data.")

    # Test Case 2: USD/JPY (Implicitly OTC), SELL, 1H (will convert to 3600s, but trader.py will enforce 300)
    another_valid_notification_regular_sell = """
🇯🇵 USD/JPY 🇨🇭
🕘 Expiration 1H
⏺ Entry at 22:15
🟥 SELL

🔼 Martingale levels
1️⃣ level at 22:20
💥 GET THIS SIGNAL HERE!
"""
    logger.info("\n--- Testing with SELL Notification (Implicitly OTC, 1H) ---")
    parsed_trade_regular_sell = parse_macrodroid_trade_data(another_valid_notification_regular_sell)
    if parsed_trade_regular_sell:
        logger.info(f"Result: {parsed_trade_regular_sell}")
    else:
        logger.error("Failed to parse another valid trade data.")

    # Test Case 3: Missing essential data (e.g., direction)
    missing_direction_notification = """
🇪🇺 EUR/USD 🇺🇸 OTC
🕘 Expiration 5M
⏺ Entry at 10:59
# Missing Direction Here #

🔼 Martingale levels
"""
    logger.info("\n--- Testing with Missing Direction Payload ---")
    parsed_trade_missing_direction = parse_macrodroid_trade_data(missing_direction_notification)
    if not parsed_trade_missing_direction:
        logger.info("Correctly failed to parse due to missing direction.")
    else:
        logger.error(f"Unexpectedly parsed data with missing direction: {parsed_trade_missing_direction}")

    # Test Case 4: Invalid Expiration format
    invalid_expiration_notification = """
🇪🇺 EUR/USD 🇺🇸 OTC
🕘 Expiration 5X
⏺ Entry at 10:59
🟩 BUY
"""
    logger.info("\n--- Testing with Invalid Expiration Payload ---")
    parsed_trade_invalid_expiration = parse_macrodroid_trade_data(invalid_expiration_notification)
    if not parsed_trade_invalid_expiration:
        logger.info("Correctly failed to parse due to invalid expiration.")
    else:
        logger.error(f"Unexpectedly parsed data with invalid expiration: {parsed_trade_invalid_expiration}")

    # Test Case 5: Asset without slashes, but still valid (implicitly OTC)
    asset_no_slash_notification = """
GBPCHF
🕘 Expiration 5M
⏺ Entry at 11:00
🟩 BUY
"""
    logger.info("\n--- Testing with Asset without Slash (Implicitly OTC) ---")
    parsed_trade_no_slash = parse_macrodroid_trade_data(asset_no_slash_notification)
    if parsed_trade_no_slash:
        logger.info(f"Result: {parsed_trade_no_slash}")
    else:
        logger.error("Failed to parse asset without slash.")