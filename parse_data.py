# parse_data.py
import re
import logging

logger = logging.getLogger(__name__)

def parse_macrodroid_trade_data(notification_text: str) -> dict:
    """
    Parses ONLY the asset pair, entry time, and direction from notification text.
    All other information is ignored.
    """
    parsed_data = {}

    # --- Asset Detection ---
    # Prioritize 6-character currency pairs (e.g., EURUSD, GBPJPY) or #STOCK formats.
    # Look anywhere in the text, case-insensitive.
    # The (?:_OTC)? makes _OTC optional if present.
    asset_match = re.search(r"([A-Z]{6}(?:_otc)?|#[A-Za-z_]+)", notification_text, re.IGNORECASE)

    if asset_match:
        asset_name = asset_match.group(1).strip().upper()
        
        # Always append "_OTC" for 6-character currency pairs if not already present.
        # This standardizes the format for Pocket Option API.
        if len(asset_name) == 6 and asset_name.isalpha() and not asset_name.endswith("_otc"):
            parsed_data["asset_name_for_po"] = asset_name + "_otc"
        else:
            parsed_data["asset_name_for_po"] = asset_name
        
        logger.info(f"Detected Asset: {asset_name} -> PO API Name: {parsed_data['asset_name_for_po']}")
    else:
        logger.warning("Could not detect Asset from notification text.")


    # --- Direction Detection ---
    # Look for BUY, SELL, CALL, or PUT anywhere, case-insensitive.
    direction_match = re.search(r"\b(BUY|SELL|CALL|PUT)\b", notification_text, re.IGNORECASE)

    if direction_match:
        direction_str = direction_match.group(1).strip().upper()
        if "BUY" in direction_str:
            parsed_data["direction"] = "CALL"
            logger.info("Detected Direction: BUY -> CALL")
        elif "SELL" in direction_str:
            parsed_data["direction"] = "PUT"
            logger.info("Detected Direction: SELL -> PUT")
        elif direction_str in ["CALL", "PUT"]:
            parsed_data["direction"] = direction_str
            logger.info(f"Detected Direction: {direction_str}")
    else:
        logger.warning("Could not detect Direction from notification text.")


    # --- Entry Time Detection ---
    # Look for HH:MM pattern anywhere in the text.
    entry_time_match = re.search(r"(\d{2}:\d{2})", notification_text)
    if entry_time_match:
        parsed_data["entryTime"] = entry_time_match.group(1).strip()
        logger.info(f"Detected Entry Time: {parsed_data['entryTime']}")
    else:
        logger.warning("Could not detect Entry Time from notification text.")

    # Duration and any other extractions are intentionally omitted as per user request.


    if parsed_data:
        logger.info(f"Successfully parsed raw notification into: {parsed_data}")
    else:
        logger.error("No trade data could be parsed from the notification. Check signal format and regex patterns.")

    return parsed_data

