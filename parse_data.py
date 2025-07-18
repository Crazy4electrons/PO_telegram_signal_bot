# parse_data.py
import re
import logging

logger = logging.getLogger(__name__)

def parse_macrodroid_trade_data(notification_text: str) -> dict:
    """
    Parses trade data from Macrodroid notification text.
    Adjust this function based on the exact format of your Macrodroid notifications.
    """
    parsed_data = {}

    # Regex patterns to capture Asset, Expiration, Entry Time, and Direction
    # Asset: Captures text between emoji/flags and "OTC" or just the asset name
    # Example: 🇪🇺 EURUSD 🇺🇸 OTC -> EURUSD
    asset_match = re.search(r"(?:🇪🇺\s*)?([A-Za-z]+(?:_[A-Za-z]+)?)\s*(?:🇺🇸\s*)?OTC", notification_text)
    if not asset_match: # Fallback for assets without OTC or flags
        asset_match = re.search(r"Asset:\s*([A-Za-z_#]+)", notification_text)

    # Expiration: Captures "5M"
    duration_match = re.search(r"Expiration\s*(\d+M)", notification_text)

    # Entry Time: Captures "09:44"
    entry_time_match = re.search(r"Entry at\s*(\d{2}:\d{2})", notification_text)

    # Direction: Captures "🟩 BUY" or "🟥 SELL"
    direction_match = re.search(r"(?:🟩\s*BUY|🟥\s*SELL)", notification_text, re.IGNORECASE)
    if not direction_match: # Fallback for direction without emoji
        direction_match = re.search(r"Direction:\s*(CALL|PUT|BUY|SELL)", notification_text, re.IGNORECASE)


    if asset_match:
        asset_name = asset_match.group(1).strip()
        # Normalize asset name for PO API (e.g., EURUSD -> EURUSDT for OTC)
        if "_otc" in asset_name.lower():
            parsed_data["asset_name_for_po"] = asset_name.replace("_otc", "T").upper()
            logger.info(f"Detected Asset: {asset_name} (Assumed OTC) -> PO API Name: {parsed_data['asset_name_for_po']}")
        else:
            # For non-OTC, if it's a currency pair like EURUSD, keep it as is
            # If it's a stock like #AAPL, keep the #
            parsed_data["asset_name_for_po"] = asset_name.upper() if not asset_name.startswith('#') else asset_name
            logger.info(f"Detected Asset: {asset_name} -> PO API Name: {parsed_data['asset_name_for_po']}")
    else:
        logger.warning("Could not detect Asset from notification text.")


    if duration_match:
        duration_text = duration_match.group(1).strip()
        parsed_data["duration_text"] = duration_text
        if duration_text.endswith("M"):
            try:
                minutes = int(duration_text[:-1])
                parsed_data["duration"] = minutes * 60 # Convert to seconds
                logger.info(f"Detected Expiration: {duration_text} ({parsed_data['duration']} seconds)")
            except ValueError:
                logger.warning(f"Could not parse duration minutes from '{duration_text}'.")
        else:
            logger.warning(f"Unsupported duration format: {duration_text}. Expected 'XM'.")
    else:
        logger.warning("Could not detect Expiration from notification text.")


    if direction_match:
        direction_str = direction_match.group(0).strip().upper() # Use group(0) to get the whole match
        # Map BUY/SELL (with or without emoji) to CALL/PUT
        if "BUY" in direction_str:
            parsed_data["direction"] = "CALL"
            logger.info("Detected Direction: BUY -> CALL")
        elif "SELL" in direction_str:
            parsed_data["direction"] = "PUT"
            logger.info("Detected Direction: SELL -> PUT")
        elif direction_str in ["CALL", "PUT"]: # Directly "CALL" or "PUT"
            parsed_data["direction"] = direction_str
            logger.info(f"Detected Direction: {direction_str}")
        else:
            logger.warning(f"Could not map direction string: {direction_str}")
    else:
        logger.warning("Could not detect Direction from notification text.")


    if entry_time_match:
        parsed_data["entryTime"] = entry_time_match.group(1).strip()
        logger.info(f"Detected Entry Time: {parsed_data['entryTime']}")
    else:
        logger.warning("Could not detect Entry Time from notification text.")

    if parsed_data:
        logger.info(f"Successfully parsed raw notification into: {parsed_data}")
    else:
        logger.error("No trade data could be parsed from the notification. Check signal format and regex patterns.")

    return parsed_data

