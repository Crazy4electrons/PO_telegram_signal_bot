# trader.py (Rewritten for FastAPI)
import os
import json
import time
import asyncio
import logging
from fastapi import FastAPI, Request, HTTPException, status # Import FastAPI components
from fastapi.responses import JSONResponse # For returning JSON responses
from pocketoptionapi_async import AsyncPocketOptionClient
from datetime import datetime, timedelta
import pytz

# Import our local parsing utility
from parse_data import parse_macrodroid_trade_data

# Configure logging for the FastAPI app and trading logic
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI() # Initialize FastAPI app

# --- Configuration Constants ---
SSID_FILE_PATH = "./shared_data/ssid.txt" 
DEFAULT_ACCOUNT_TYPE = "PRACTICE" 
FIXED_TRADE_DURATION_SECONDS = 300 
INITIAL_TRADE_AMOUNT = 1.0
MARTINGALE_MULTIPLIER = 2.0
MAX_MARTINGALE_LEVELS = 2 

# --- Timezone Configuration ---
SIGNAL_TIMEZONE = pytz.timezone('America/New_York')
LOCAL_TIMEZONE = pytz.timezone('Africa/Windhoek')

# --- Global State Management ---
trade_sequence_state = {
    "active": False,
    "asset": None,
    "direction": None,
    "current_level": 0,
    "current_amount": INITIAL_TRADE_AMOUNT,
    "last_trade_id": None,
    "last_trade_status": None
}

# Pocket Option API client instance - now managed directly as a global
pocket_option_api = None

# --- Utility Functions ---

def read_ssid_from_file(retries=10, delay_seconds=5):
    """
    Reads the SSID from the shared file with retries.
    """
    for i in range(retries):
        try:
            if os.path.exists(SSID_FILE_PATH):
                with open(SSID_FILE_PATH, "r") as f:
                    ssid = f.read().strip()
                if ssid:
                    logger.info(f"SSID successfully read from {SSID_FILE_PATH}")
                    return ssid
            logger.warning(f"SSID file not found or empty: {SSID_FILE_PATH}. Retrying in {delay_seconds}s...")
        except Exception as e:
            logger.error(f"Error reading SSID from file {SSID_FILE_PATH}: {e}. Retrying in {delay_seconds}s...")
        time.sleep(delay_seconds)
    logger.critical(f"Failed to read SSID from {SSID_FILE_PATH} after {retries} attempts. Please ensure scraper.py is running.")
    return None

async def connect_to_pocket_option():
    """
    Establishes and manages the connection to Pocket Option API using the SSID.
    Ensures only one active connection.
    """
    global pocket_option_api

    if pocket_option_api and pocket_option_api.check_connection():
        logger.info("Pocket Option API client already connected.")
        return True

    ssid = read_ssid_from_file()
    if not ssid:
        logger.error("Pocket Option SSID not available. Cannot connect.")
        return False

    logger.info("Attempting to connect to Pocket Option using SSID.")
    pocket_option_api = AsyncPocketOptionClient(ssid, is_demo=(DEFAULT_ACCOUNT_TYPE == "PRACTICE"))

    try:
        connected = await pocket_option_api.connect()
        if not connected:
            logger.error("Pocket Option login failed. Check PocketOptionAPI logs for details (e.g., Authentication timeout).")
            pocket_option_api = None 
            return False
        logger.info("Successfully connected to PocketOption API.")

        await pocket_option_api.set_act(DEFAULT_ACCOUNT_TYPE)
        logger.info(f"Switched to {DEFAULT_ACCOUNT_TYPE} account.")

        balance = await pocket_option_api.get_balance()
        logger.info(f"Current {DEFAULT_ACCOUNT_TYPE} Balance: {balance}")
        return True
    except Exception as e:
        logger.critical(f"An unexpected error occurred during Pocket Option connection: {e}", exc_info=True)
        pocket_option_api = None 
        return False

async def place_trade(asset: str, direction: str, amount: float, duration: int):
    """
    Places a trade on Pocket Option.
    Returns (True, trade_id) on success, (False, error_message) on failure.
    """
    if not pocket_option_api or not pocket_option_api.check_connection():
        logger.error("Pocket Option API not connected. Cannot place trade.")
        return False, "API not connected"

    logger.info(f"Attempting to place trade: Asset={asset}, Direction={direction}, Amount=${amount}, Duration={duration}s")
    try:
        status, trade_id = await pocket_option_api.buy(
            amount=amount,
            asset=asset,
            action=direction,
            timeframe=duration
        )

        if status:
            logger.info(f"Trade successfully placed! Asset: {asset}, Direction: {direction}, Amount: ${amount}, Trade ID: {trade_id}")
            return True, trade_id
        else:
            logger.error(f"Failed to place trade for {asset} ({direction} ${amount}): {trade_id}")
            return False, trade_id
    except Exception as e:
        logger.error(f"Error placing trade: {e}", exc_info=True)
        return False, str(e)

async def monitor_trade_outcome(trade_id: int, expected_duration: int):
    """
    Monitors the outcome of a single trade.
    """
    logger.info(f"Monitoring trade ID: {trade_id}. Waiting for trade to expire and result to be available...")
    
    await asyncio.sleep(expected_duration + 15) # Wait for trade duration + buffer

    max_retries = 5
    retry_delay = 3 # seconds
    for attempt in range(max_retries):
        try:
            if not pocket_option_api or not pocket_option_api.check_connection():
                logger.error("API not connected while monitoring trade outcome. Cannot get result.")
                return "error"

            # --- SIMULATED OUTCOME ---
            import random
            outcome = random.choice(["win", "loss"]) # Simulate outcome
            logger.info(f"Simulated outcome for trade ID {trade_id} (Attempt {attempt+1}/{max_retries}): {outcome}")
            return outcome
            # --- END SIMULATED OUTCOME ---

        except Exception as e:
            logger.warning(f"Error fetching trade history for ID {trade_id} (Attempt {attempt+1}/{max_retries}): {e}")
            await asyncio.sleep(retry_delay)
    
    logger.error(f"Failed to get outcome for trade ID {trade_id} after {max_retries} attempts.")
    return "error"

async def handle_trade_outcome_and_martingale(trade_id: int, duration: int, asset: str, direction: str, amount: float):
    """
    Handles the outcome of a trade and applies Martingale logic based on the global state.
    This function runs as a separate asyncio task after a trade is placed.
    """
    global trade_sequence_state

    outcome = await monitor_trade_outcome(trade_id, duration)
    trade_sequence_state["last_trade_status"] = outcome

    logger.info(f"Outcome for trade ID {trade_id} ({asset} {direction} ${amount}): {outcome}")

    if outcome == "win":
        logger.info(f"Trade WIN for {asset} {direction} ${amount}. Resetting Martingale sequence.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
    elif outcome == "loss":
        if trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
            logger.info(f"Trade LOSS for {asset} {direction} ${amount}. Waiting for next signal for re-entry.")
        else:
            logger.info(f"Trade LOSS for {asset} {direction} ${amount} at final Martingale level ({MAX_MARTINGALE_LEVELS + 1}). Resetting sequence.")
            trade_sequence_state["active"] = False
            trade_sequence_state["current_level"] = 0
            trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
    else: # "error" or unexpected outcome
        logger.error(f"Error or unexpected outcome '{outcome}' for trade ID {trade_id}. Resetting Martingale sequence to avoid issues.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT

# --- FastAPI Endpoint ---

@app.post('/trade_signal') # Use @app.post for POST requests
async def trade_signal_webhook(request: Request): # FastAPI uses Request object
    """
    Receives trade signals from Macrodroid (via Ngrok), parses them,
    and executes trades with Martingale logic and precise timing.
    """
    global trade_sequence_state

    # Read raw body directly from FastAPI Request object
    raw_notification_text = (await request.body()).decode('utf-8')
    logger.info(f"Received raw notification from Macrodroid:\n{raw_notification_text}")

    parsed_data = parse_macrodroid_trade_data(raw_notification_text)

    if not parsed_data:
        logger.error("Failed to parse trade data from notification. Aborting trade attempt.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to parse trade data from notification.")

    signal_asset = parsed_data.get("asset_name_for_po")
    signal_direction = parsed_data.get("direction")
    signal_entry_time_str = parsed_data.get("entryTime")
    trade_duration = FIXED_TRADE_DURATION_SECONDS

    if not signal_entry_time_str:
        logger.error("Signal is missing 'Entry at HH:MM'. Cannot determine precise entry time. Aborting.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Signal missing entry time.")

    # --- Timing Logic: Calculate Target Local Time ---
    current_local_dt = datetime.now(LOCAL_TIMEZONE)
    
    try:
        signal_time_obj = datetime.strptime(signal_entry_time_str, "%H:%M").time()
        signal_dt_in_signal_tz = SIGNAL_TIMEZONE.localize(
            datetime(current_local_dt.year, current_local_dt.month, current_local_dt.day,
                     signal_time_obj.hour, signal_time_obj.minute, 0)
        )
        target_local_dt = signal_dt_in_signal_tz.astimezone(LOCAL_TIMEZONE)
    except Exception as e:
        logger.error(f"Error parsing or converting signal entry time '{signal_entry_time_str}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid signal entry time format: {e}")

    logger.info(f"Signal entry time (GMT-4): {signal_entry_time_str}. Calculated local target entry time: {target_local_dt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")

    if current_local_dt > target_local_dt + timedelta(seconds=5):
        logger.warning(f"Signal for {signal_asset} {signal_direction} (Entry: {signal_entry_time_str}) arrived late. Current local time: {current_local_dt.strftime('%H:%M:%S')}, Target local time: {target_local_dt.strftime('%H:%M:%S')}. Skipping trade.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "skipped", "message": "Signal arrived too late, trade skipped."})

    time_to_wait_seconds = (target_local_dt - current_local_dt).total_seconds()

    if time_to_wait_seconds > 0:
        logger.info(f"Waiting {time_to_wait_seconds:.2f} seconds until target entry time: {target_local_dt.strftime('%H:%M:%S')}")
        await asyncio.sleep(time_to_wait_seconds)
        logger.info(f"Reached target entry time. Proceeding with trade for {signal_asset} {signal_direction}.")
    else:
        logger.info(f"Signal arrived exactly at or slightly past target entry time ({current_local_dt.strftime('%H:%M:%S')} vs {target_local_dt.strftime('%H:%M:%S')}). Placing trade immediately.")


    # --- Martingale Strategy Logic ---
    is_new_signal_type = (trade_sequence_state["asset"] != signal_asset or 
                          trade_sequence_state["direction"] != signal_direction)

    if is_new_signal_type or not trade_sequence_state["active"] or \
       (trade_sequence_state["active"] and trade_sequence_state["last_trade_status"] == "win") or \
       (trade_sequence_state["active"] and trade_sequence_state["last_trade_status"] == "loss" and trade_sequence_state["current_level"] >= MAX_MARTINGALE_LEVELS):
        
        logger.info(f"Starting new trade sequence for {signal_asset} {signal_direction}. Initial Amount: ${INITIAL_TRADE_AMOUNT:.2f}")
        trade_sequence_state = {
            "active": True,
            "asset": signal_asset,
            "direction": signal_direction,
            "current_level": 0,
            "current_amount": INITIAL_TRADE_AMOUNT,
            "last_trade_id": None,
            "last_trade_status": "pending"
        }
    elif trade_sequence_state["active"] and \
         trade_sequence_state["asset"] == signal_asset and \
         trade_sequence_state["direction"] == signal_direction and \
         trade_sequence_state["last_trade_status"] == "loss" and \
         trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
        
        trade_sequence_state["current_level"] += 1
        trade_sequence_state["current_amount"] *= MARTINGALE_MULTIPLIER
        logger.info(f"Continuing Martingale for {signal_asset} {signal_direction}. Level {trade_sequence_state['current_level']+1}. Amount: ${trade_sequence_state['current_amount']:.2f}")
        trade_sequence_state["last_trade_status"] = "pending"
    else:
        logger.warning(f"Unexpected Martingale state for {signal_asset} {signal_direction}. Resetting sequence.")
        trade_sequence_state = {
            "active": True,
            "asset": signal_asset,
            "direction": signal_direction,
            "current_level": 0,
            "current_amount": INITIAL_TRADE_AMOUNT,
            "last_trade_id": None,
            "last_trade_status": "pending"
        }

    # Ensure connection to Pocket Option API
    if not await connect_to_pocket_option():
        logger.error("Could not establish or re-establish connection to Pocket Option. Aborting trade.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Pocket Option API connection failed.")

    success, trade_info = await place_trade(
        asset=trade_sequence_state["asset"],
        direction=trade_sequence_state["direction"],
        amount=trade_sequence_state["current_amount"],
        duration=trade_duration
    )

    if success:
        trade_sequence_state["last_trade_id"] = trade_info
        logger.info(f"Trade placed. Now initiating outcome monitoring for trade ID: {trade_sequence_state['last_trade_id']}")
        
        asyncio.create_task(
            handle_trade_outcome_and_martingale(
                trade_sequence_state["last_trade_id"],
                trade_duration,
                trade_sequence_state["asset"],
                trade_sequence_state["direction"],
                trade_sequence_state["current_amount"]
            )
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "trade_placed",
            "message": "Trade placed successfully. Outcome will be processed shortly.",
            "trade_id": trade_sequence_state["last_trade_id"],
            "asset": trade_sequence_state["asset"],
            "direction": trade_sequence_state["direction"],
            "amount": trade_sequence_state["current_amount"],
            "martingale_level": trade_sequence_state["current_level"] + 1
        })
    else:
        logger.error(f"Failed to place trade: {trade_info}. Resetting Martingale sequence due to placement failure.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to place trade: {trade_info}")

# --- Application Startup Event (FastAPI equivalent of before_serving) ---
@app.on_event("startup")
async def startup_event():
    """
    FastAPI startup event to initialize the PocketOption API client.
    """
    logger.info("FastAPI startup event: Initializing PocketOption API client.")
    success = await connect_to_pocket_option() # Use the connect_to_pocket_option function
    if not success:
        logger.critical("Failed to initialize PocketOption API client on startup. Trading will not work.")
    else:
        logger.info("PocketOption API client initialized successfully during startup.")

# No __name__ == '__main__' block for FastAPI, Uvicorn runs it directly.