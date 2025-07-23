import os
import json
import time
import asyncio
import pytz
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from typing import Optional, AsyncIterator, Any
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
from pocketoptionapi_async.models import OrderResult, Balance # Import Balance model for type hinting

# Assuming parse_data.py is correctly implemented and available
from parse_data import parse_macrodroid_trade_data

load_dotenv()

# Simplified logging configuration to reduce verbosity
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s') # Removed %(levelname)s
logger = logging.getLogger(__name__)

# Configure the logger for pocketoptionapi_async to suppress DEBUG messages
pocket_option_logger = logging.getLogger('pocketoptionapi_async')
pocket_option_logger.setLevel(logging.INFO) # Set to INFO to show INFO, WARNING, ERROR, CRITICAL

pocket_option_client: Optional[AsyncPocketOptionClient] = None
is_demo_session: Optional[bool] = None
# A flag to ensure only one trade sequence (Martingale included) is active globally
is_processing_trade_sequence: bool = False

FIXED_TRADE_DURATION_SECONDS = 300 # 5 minutes
INITIAL_TRADE_AMOUNT = 1.0
MARTINGALE_MULTIPLIER = 2.0
MAX_MARTINGALE_LEVELS = 2 # Max Martingale levels after the initial trade (0-indexed). So 0=initial, 1=1st Martingale, 2=2nd Martingale (total 3 trades max).

SIGNAL_TIMEZONE = pytz.timezone('America/New_York')
LOCAL_TIMEZONE = pytz.timezone('Africa/Windhoek')

trade_sequence_state = {
    "active": False,
    "asset": None,
    "direction": None,
    "current_level": 0, # 0 for initial trade, 1 for first martingale, etc.
    "current_amount": INITIAL_TRADE_AMOUNT,
    "last_trade_id": None,
    "last_trade_status": None, # "win", "loss", "tie", "pending", "uncertain"
    "balance_before_current_trade": None, # Store balance before the trade for outcome determination
}

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global pocket_option_client, is_demo_session

    logger.info("FastAPI lifespan startup event: Initializing Pocket Option client.")

    while True:
        user_choice = input("Enter account type to use for trading (DEMO/REAL): ").strip().upper()
        if user_choice == "DEMO":
            is_demo_session = True
            logger.info("Selected DEMO account for trading session.")
            break
        elif user_choice == "REAL":
            is_demo_session = False
            logger.info("Selected REAL account for trading session.")
            break
        else:
            print("Invalid input. Please enter 'DEMO' or 'REAL'.")

    ssid = os.getenv('SSID')
    uid = os.getenv('UID')
    
    if not ssid:
        logger.error("SSID not found in .env. Please ensure scraper.py has run or .env is correctly set.")
        yield
        return
    if not uid:
        logger.error("UID not found in .env. Please ensure scraper.py has run or .env is correctly set.")
        yield
        return

    pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session)

    try:
        await pocket_option_client.connect()
        logger.info("Pocket Option client connected successfully on startup.")
        
        # Balance check at application startup
        initial_balance_obj = await get_valid_balance(pocket_option_client, "startup")
        if initial_balance_obj:
            logger.info(f'Balance at startup: {initial_balance_obj.balance} {initial_balance_obj.currency} (Is Demo: {initial_balance_obj.is_demo})')
        else:
            logger.error("Failed to retrieve a valid balance at startup.")

    except Exception as e:
        logger.error(f"Initial Pocket Option client connection failed on startup: {e}")
        pocket_option_client = None
    
    yield

    logger.info("FastAPI lifespan shutdown event: Disconnecting Pocket Option client.")
    if pocket_option_client:
        await pocket_option_client.disconnect()
        logger.info("Pocket Option client disconnected during shutdown.")

app = FastAPI(lifespan=lifespan)

async def get_valid_balance(client: AsyncPocketOptionClient, context: str = "general") -> Optional[Balance]:
    """
    Attempts to retrieve a valid (non-zero, non-None) balance from the client.
    Retries multiple times if an invalid balance is returned.
    """
    MAX_BALANCE_RETRIES = 10
    BALANCE_RETRY_INTERVAL = 1 # seconds
    
    for i in range(MAX_BALANCE_RETRIES):
        try:
            balance_obj = await client.get_balance()
            if balance_obj and balance_obj.balance is not None and balance_obj.balance > 0:
                return balance_obj
            else:
                logger.warning(f"[{context}] Retrieved invalid balance ({balance_obj.balance if balance_obj else 'None'}). Retrying in {BALANCE_RETRY_INTERVAL}s...")
        except Exception as e:
            logger.warning(f"[{context}] Error getting balance (attempt {i+1}/{MAX_BALANCE_RETRIES}): {e}. Retrying...")
        await asyncio.sleep(BALANCE_RETRY_INTERVAL)
    
    logger.error(f"[{context}] Failed to retrieve a valid balance after {MAX_BALANCE_RETRIES} attempts.")
    return None


@app.post('/trade_signal')
async def trade_signal_webhook(request: Request) -> JSONResponse:
    global trade_sequence_state, pocket_option_client, is_demo_session, is_processing_trade_sequence

    # --- Ensure only one trade sequence is active at a time ---
    if is_processing_trade_sequence:
        logger.info(f"Signal ignored. Another trade sequence active (Asset: {trade_sequence_state['asset']}, Level: {trade_sequence_state['current_level']}).")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "ignored",
            "message": "Signal ignored. Another trade sequence is currently in progress."
        })

    if not pocket_option_client or not pocket_option_client.is_connected:
        logger.error("Pocket Option client is not connected. Attempting reconnection.")
        if await connect_pocket_option_client():
            logger.info("Re-established Pocket Option connection for trade signal.")
        else:
            logger.error("Failed to re-establish Pocket Option connection. Aborting trade signal.")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Pocket Option API not connected and reconnection failed.")

    raw_notification_text = (await request.body()).decode('utf-8')

    parsed_data = parse_macrodroid_trade_data(raw_notification_text)
    trade_duration = FIXED_TRADE_DURATION_SECONDS # Always use the fixed duration (5 minutes)

    if not parsed_data.get("asset_name_for_po") or not parsed_data.get("direction") or not parsed_data.get("entryTime"):
        logger.error("Failed to parse essential trade data. Aborting.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to parse essential trade data.")

    signal_asset = parsed_data["asset_name_for_po"]
    signal_direction_str = parsed_data["direction"]
    signal_entry_time_str = parsed_data["entryTime"]

    try:
        signal_direction = OrderDirection[signal_direction_str.upper()]
    except (KeyError, AttributeError):
        logger.error(f"Invalid trade direction: '{signal_direction_str}'. Must be 'CALL' or 'PUT'.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid trade direction.")

    current_local_dt = datetime.now(LOCAL_TIMEZONE)
    
    try:
        signal_time_obj = datetime.strptime(signal_entry_time_str, "%H:%M").time()
        signal_dt_in_signal_tz = SIGNAL_TIMEZONE.localize(
            datetime(current_local_dt.year, current_local_dt.month, current_local_dt.day,
                     signal_time_obj.hour, signal_time_obj.minute, 0)
        )
        target_local_dt = signal_dt_in_signal_tz.astimezone(LOCAL_TIMEZONE)
    except Exception as e:
        logger.error(f"Error parsing signal entry time '{signal_entry_time_str}': {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid signal entry time format: {e}")

    logger.info(f"Signal for {signal_asset} {signal_direction.value}. Entry: {signal_entry_time_str} (GMT-4). Local target: {target_local_dt.strftime('%H:%M:%S')}")

    if current_local_dt > target_local_dt + timedelta(seconds=5):
        logger.info(f"Signal for {signal_asset} arrived late. Current: {current_local_dt.strftime('%H:%M:%S')}, Target: {target_local_dt.strftime('%H:%M:%S')}. Skipping trade.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "skipped", "message": "Signal arrived too late, trade skipped."})

    time_to_wait_seconds = (target_local_dt - current_local_dt).total_seconds()

    logger.info(f"Initiating new trade sequence for {signal_asset} {signal_direction.value}. Initial Amount: ${INITIAL_TRADE_AMOUNT:.2f}")
    
    is_processing_trade_sequence = True
    trade_sequence_state.update({
        "active": True,
        "asset": signal_asset,
        "direction": signal_direction,
        "current_level": 0,
        "current_amount": INITIAL_TRADE_AMOUNT,
        "last_trade_id": None,
        "last_trade_status": "pending",
    })

    # --- Get balance BEFORE placing the initial trade ---
    balance_before_trade_obj = await get_valid_balance(pocket_option_client, "before_initial_trade")
    if not balance_before_trade_obj:
        logger.error("Failed to get valid balance before initial trade. Aborting trade sequence.")
        reset_trade_sequence_state()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Failed to get valid balance before trade.")
    trade_sequence_state["balance_before_current_trade"] = balance_before_trade_obj.balance
    logger.info(f"Balance before initial trade: {trade_sequence_state['balance_before_current_trade']} {balance_before_trade_obj.currency}")


    if time_to_wait_seconds > 0.05:
        logger.info(f"Waiting {time_to_wait_seconds:.2f}s until {target_local_dt.strftime('%H:%M:%S')}.")
        await asyncio.sleep(time_to_wait_seconds)
        logger.info(f"Reached target entry time. Placing trade for {signal_asset} {signal_direction.value}.")
    else:
        logger.info(f"Placing trade immediately for {signal_asset} {signal_direction.value}.")

    try:
        order = await pocket_option_client.place_order(
            asset=trade_sequence_state["asset"],
            amount=trade_sequence_state["current_amount"],
            direction=trade_sequence_state["direction"],
            duration=trade_duration
        )
        logger.info(f"Order placed! Trade ID: {order.order_id}, Status: {order.status}")
        trade_sequence_state["last_trade_id"] = order.order_id
        
        # Balance check after initial trade placement (for logging only)
        try:
            balance_after_trade_obj = await get_valid_balance(pocket_option_client, "after_initial_trade_placement")
            if balance_after_trade_obj:
                logger.info(f"Balance after initial trade placement: {balance_after_trade_obj.balance} {balance_after_trade_obj.currency}")
        except Exception as e:
            logger.warning(f"Could not retrieve balance after initial trade placement: {e}")

        asyncio.create_task(
            monitor_trade_and_execute_martingale(
                trade_sequence_state["last_trade_id"],
                trade_duration,
                trade_sequence_state["asset"],
                trade_sequence_state["direction"],
                trade_sequence_state["current_amount"], # Amount invested in THIS trade
                trade_sequence_state["balance_before_current_trade"] # Balance before THIS trade
            )
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "initial_trade_placed",
            "message": "Initial trade placed successfully.",
            "trade_id": trade_sequence_state["last_trade_id"],
            "asset": trade_sequence_state["asset"],
            "direction": trade_sequence_state["direction"].value,
            "amount": trade_sequence_state["current_amount"],
            "martingale_level": trade_sequence_state["current_level"]
        })
    except Exception as e:
        logger.error(f"Failed to place initial trade: {e}")
        # Reset sequence and global flag on failure to place initial trade
        reset_trade_sequence_state()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to place initial trade: {e}")


async def connect_pocket_option_client() -> bool:
    global pocket_option_client, is_demo_session

    if pocket_option_client and pocket_option_client.is_connected:
        return True

    ssid = os.getenv('SSID')
    uid = os.getenv('UID')
    
    if not ssid or not uid or is_demo_session is None:
        logger.error("Missing environment variables or account type for connection.")
        return False

    try:
        if not pocket_option_client:
            pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session,enable_logging=False)
            
        await pocket_option_client.connect()
        return True
    except Exception as e:
        logger.error(f"Failed to re-connect Pocket Option client: {e}")
        pocket_option_client = None
        return False


async def monitor_trade_and_execute_martingale(trade_id: int, duration: int, asset: str, direction: OrderDirection, invested_amount: float, balance_before_this_trade: float) -> None:
    global trade_sequence_state, pocket_option_client, is_processing_trade_sequence

    # Wait for the full trade duration to elapse
    logger.info(f"Trade ID {trade_id} (Level {trade_sequence_state['current_level']}). Waiting {duration}s for trade to conclude.")
    await asyncio.sleep(duration)
    
    # --- Step 1: Get the profit amount for this specific trade from OrderResult ---
    trade_details_for_profit: Optional[OrderResult] = None
    MAX_DETAILS_RETRIES = 15 # Increased retries for getting profit details
    DETAILS_RETRY_INTERVAL = 1 # seconds
    
    for i in range(MAX_DETAILS_RETRIES):
        try:
            details = await pocket_option_client.check_order_result(trade_id)
            if details and details.profit is not None:
                trade_details_for_profit = details
                logger.info(f"Trade details for profit retrieved for ID {trade_id}: Profit={details.profit}, Amount={details.amount}")
                break
            else:
                logger.warning(f"Trade ID {trade_id}: Details (profit) not yet available or incomplete. Retrying in {DETAILS_RETRY_INTERVAL}s...")
        except Exception as e:
            logger.warning(f"Error getting trade details for profit for ID {trade_id} (attempt {i+1}/{MAX_DETAILS_RETRIES}): {type(e).__name__}: {e}. Retrying...")
        await asyncio.sleep(DETAILS_RETRY_INTERVAL)

    if trade_details_for_profit is None or trade_details_for_profit.profit is None:
        logger.error(f"Trade ID {trade_id}: Failed to retrieve trade details for profit after {MAX_DETAILS_RETRIES} attempts. Cannot accurately determine win/loss by balance. Assuming loss.")
        martingale_reentry_needed = True
        # If profit details are missing, assume balance would decrease by invested_amount on loss
        trade_sequence_state["balance_before_current_trade"] = balance_before_this_trade - invested_amount
        await execute_martingale_or_reset(trade_id, duration, asset, direction, invested_amount, martingale_reentry_needed)
        return

    profit_amount_if_won = trade_details_for_profit.profit

    # --- Step 2: Get current balance after trade conclusion (with retry for non-zero) ---
    current_balance_obj = await get_valid_balance(pocket_option_client, f"after_trade_{trade_id}")
    if not current_balance_obj:
        logger.error(f"Trade ID {trade_id}: Failed to retrieve valid current balance after trade. Cannot determine trade outcome. Assuming loss.")
        martingale_reentry_needed = True
        # If current balance cannot be retrieved, assume loss and update balance accordingly
        trade_sequence_state["balance_before_current_trade"] = balance_before_this_trade - invested_amount
        await execute_martingale_or_reset(trade_id, duration, asset, direction, invested_amount, martingale_reentry_needed)
        return
    current_balance = current_balance_obj.balance

    # --- Step 3: Determine outcome based on balance change ---
    expected_balance_if_loss = balance_before_this_trade - invested_amount
    # Updated: For a win, the final balance is the initial balance + net profit + invested amount returned
    expected_balance_if_win = balance_before_this_trade + profit_amount_if_won + invested_amount 

    # Use a small tolerance for floating point comparisons
    TOLERANCE = 0.01 # 1 cent tolerance

    martingale_reentry_needed = False
    if abs(current_balance - expected_balance_if_win) < TOLERANCE:
        logger.info(f"Trade ID {trade_id}: Determined WIN by balance. Current: {current_balance}, Expected Win: {expected_balance_if_win:.2f}")
        martingale_reentry_needed = False
        trade_sequence_state["last_trade_status"] = "win"
    elif abs(current_balance - expected_balance_if_loss) < TOLERANCE:
        logger.info(f"Trade ID {trade_id}: Determined LOSS by balance. Current: {current_balance}, Expected Loss: {expected_balance_if_loss:.2f}")
        martingale_reentry_needed = True
        trade_sequence_state["last_trade_status"] = "lose"
    else:
        logger.warning(f"Trade ID {trade_id}: Balance {current_balance} does not match expected WIN ({expected_balance_if_win:.2f}) or LOSS ({expected_balance_if_loss:.2f}). Assuming LOSS for Martingale.")
        logger.warning(f"Debug: Balance before trade: {balance_before_this_trade}, Invested: {invested_amount}, Profit if won: {profit_amount_if_won}")
        martingale_reentry_needed = True
        trade_sequence_state["last_trade_status"] = "uncertain_loss" # Custom status for logging

    # --- Step 4: Update balance_before_current_trade for the next Martingale level (if applicable) ---
    trade_sequence_state["balance_before_current_trade"] = current_balance

    await execute_martingale_or_reset(trade_id, duration, asset, direction, invested_amount, martingale_reentry_needed)


async def execute_martingale_or_reset(trade_id: int, duration: int, asset: str, direction: OrderDirection, amount: float, martingale_reentry_needed: bool):
    global trade_sequence_state, pocket_option_client, is_processing_trade_sequence

    if martingale_reentry_needed:
        logger.info(f"Trade ID {trade_id}: Official LOSS (or uncertain). Martingale Level: {trade_sequence_state['current_level']}.")
        if trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
            trade_sequence_state["current_level"] += 1
            trade_sequence_state["current_amount"] *= MARTINGALE_MULTIPLIER
            
            logger.info(f"Placing Martingale Level {trade_sequence_state['current_level']} trade for {asset} {direction.value}. Amount: ${trade_sequence_state['current_amount']:.2f}")

            try:
                next_order = await pocket_option_client.place_order(
                    asset=trade_sequence_state["asset"],
                    amount=trade_sequence_state["current_amount"],
                    direction=trade_sequence_state["direction"],
                    duration=duration
                )
                logger.info(f"Martingale Level {trade_sequence_state['current_level']} trade placed! Order ID: {next_order.order_id}, Status: {next_order.status}")
                trade_sequence_state["last_trade_id"] = next_order.order_id
                
                # Update balance_before_current_trade for the newly placed Martingale trade
                balance_after_martingale_placement_obj = await get_valid_balance(pocket_option_client, "after_martingale_placement")
                if balance_after_martingale_placement_obj:
                    trade_sequence_state["balance_before_current_trade"] = balance_after_martingale_placement_obj.balance
                    logger.info(f"Balance after Martingale trade placement: {balance_after_martingale_placement_obj.balance} {balance_after_martingale_placement_obj.currency}")
                else:
                    logger.error("Failed to get valid balance after Martingale trade placement. This might affect next outcome determination.")


                # Continue monitoring this new Martingale trade
                asyncio.create_task(
                    monitor_trade_and_execute_martingale(
                        trade_sequence_state["last_trade_id"],
                        duration,
                        trade_sequence_state["asset"],
                        trade_sequence_state["direction"],
                        trade_sequence_state["current_amount"], # Amount invested in this new Martingale trade
                        trade_sequence_state["balance_before_current_trade"] # Balance before this new Martingale trade
                    )
                )
            except Exception as e:
                logger.error(f"Failed to place Martingale Level {trade_sequence_state['current_level']} trade: {e}. Resetting sequence.")
                reset_trade_sequence_state()
        else:
            logger.info(f"Trade LOSS (official) for {asset} at final Martingale level {MAX_MARTINGALE_LEVELS}. Resetting sequence. Waiting for next signal.")
            reset_trade_sequence_state()
    else: # Official WIN or TIE
        logger.info(f"Official WIN/TIE for Trade ID {trade_id}. Resetting Martingale sequence.")
        reset_trade_sequence_state()
    

def reset_trade_sequence_state():
    global trade_sequence_state, is_processing_trade_sequence
    trade_sequence_state.update({
        "active": False,
        "asset": None,
        "direction": None,
        "current_level": 0,
        "current_amount": INITIAL_TRADE_AMOUNT,
        "last_trade_id": None,
        "last_trade_status": None,
        "balance_before_current_trade": None,
    })
    is_processing_trade_sequence = False # Release the global lock