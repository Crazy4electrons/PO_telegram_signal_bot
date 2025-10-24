from math import log
import os
import json
import time
import asyncio
import logging
import pytz
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from datetime import date, datetime, timedelta
from typing import Optional, AsyncIterator, Any
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
from pocketoptionapi_async.models import OrderResult, Candle # Import Candle model

# Assuming parse_data.py is correctly implemented and available
from parse_data import parse_macrodroid_trade_data
from measure_latency import measure_one

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

pocket_option_client: Optional[AsyncPocketOptionClient] = None
is_demo_session: Optional[bool] = os.getenv('ACCOUNT_TYPE', 'DEMO').upper() == 'DEMO'  # Default to DEMO if not set
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
    "last_trade_status": None, # "win", "loss", "tie", "pending"
    "profit": None, 
    "current_balance": None # Updated before each trade
}

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global pocket_option_client, is_demo_session

    logger.info("FastAPI lifespan startup event: Initializing Pocket Option client.")

    while True:
        # user_choice = input("Enter account type to use for trading (DEMO/REAL): ").strip().upper()
        # if user_choice == "DEMO":
        if is_demo_session:
            # is_demo_session = True
            logger.info("Selected DEMO account for trading session.")
            break
        # elif user_choice == "REAL":
        elif not is_demo_session:
            # is_demo_session = False
            logger.info("Selected REAL account for trading session.")
            break
        else:
            print("Invalid input. Please enter 'DEMO' or 'REAL'.")

    ssid = os.getenv('SSID')
    uid = os.getenv('UID') # UID checked, but not directly used for connection in this file.
    
    if not ssid:
        logger.critical("SSID not found in .env. Please ensure scraper.py has run or .env is correctly set.")
        yield
        return
    if not uid:
        logger.critical("UID not found in .env. Please ensure scraper.py has run or .env is correctly set.")
        yield
        return
    
    pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session,enable_logging=False) # type: ignore
    for i in range(10):
        try:
            await pocket_option_client.connect()
            balance = await pocket_option_client.get_balance()
            logger.info(f'Pocket Option client connected successfully on startup. Balance: {balance.balance} {balance.currency} (Is Demo: {balance.is_demo})')
            break
        except Exception as e:
            logger.error("Failed to connect Pocket Option client.")
            if i == 10:
                logger.critical(f"Initial Pocket Option client connection failed on startup: {e}", exc_info=True)
                yield 
                return
            logger.info(f"Retrying Pocket Option connection in 5 seconds..              retry attempt: {str(i + 1) } /10.")
            await asyncio.sleep(5)
    
    yield

    logger.info("FastAPI lifespan shutdown event: Disconnecting Pocket Option client.")
    if pocket_option_client:
        await pocket_option_client.disconnect()
        logger.info("Pocket Option client disconnected during shutdown.")

app = FastAPI(lifespan=lifespan)

@app.post('/trade_signal')
async def trade_signal_webhook(request: Request) -> JSONResponse:
    global trade_sequence_state, pocket_option_client, is_demo_session, is_processing_trade_sequence
    stats = pocket_option_client.get_connection_stats() # type: ignore
    logger.info(f"Pocket Option connection stats: {stats}")
    # --- Ensure only one trade sequence is active at a time ---
    if is_processing_trade_sequence:
        logger.warning(f"Received new signal while a trade sequence is already active "
                       f" (Asset: {trade_sequence_state['asset']}, Direction: {trade_sequence_state['direction'].value if trade_sequence_state['direction'] else 'N/A'}, "
                       f" Level: {trade_sequence_state['current_level']}). "
                       f" Ignoring new signal and waiting for current sequence to complete.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "ignored",
            "message": "Signal ignored. Another trade sequence is currently in progress."
        })
    # ----- Ensure connection to pocket option -----
    if not pocket_option_client or not pocket_option_client.is_connected:
        logger.error("Pocket Option client is not connected. Attempting to re-establish connection.")
        if await connect_pocket_option_client():
            logger.info("Re-established Pocket Option connection for trade signal.")
        else:
            logger.critical("Failed to re-establish Pocket Option connection. Aborting trade signal processing.")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Pocket Option API not connected and reconnection failed.")
        
    # --- Parse incoming notification ---
    raw_notification_text = (await request.body()).decode('utf-8')
    logger.info(f"Received raw notification from Macrodroid:\n{raw_notification_text}")

    parsed_data = parse_macrodroid_trade_data(raw_notification_text)
    trade_duration = FIXED_TRADE_DURATION_SECONDS # Always use the fixed duration (5 minutes)

    if not parsed_data.get("asset_name_for_po") or not parsed_data.get("direction") or not parsed_data.get("entryTime"):
        logger.error("Failed to parse essential trade data (asset, direction, or entry time) from notification. Aborting trade attempt.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to parse essential trade data from notification.")

    signal_asset = parsed_data["asset_name_for_po"]
    signal_direction_str = parsed_data["direction"]
    signal_entry_time_str = parsed_data["entryTime"]

    try:
        signal_direction = OrderDirection[signal_direction_str.upper()]
    except (KeyError, AttributeError):
        logger.error(f"Invalid or missing trade direction received: '{signal_direction_str}'. Must be 'CALL' or 'PUT'.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid trade direction.")

    current_local_dt = datetime.now(LOCAL_TIMEZONE)
        
    try:
        signal_time_obj = datetime.strptime(signal_entry_time_str, "%H:%M").time()
        signal_dt_in_signal_tz = SIGNAL_TIMEZONE.localize(
            datetime(current_local_dt.year, current_local_dt.month, current_local_dt.day,
                 signal_time_obj.hour, signal_time_obj.minute, 0)
        )
        
        # Check if local time is before 6 AM
        if current_local_dt.hour <= 6:
            signal_dt_in_signal_tz = signal_dt_in_signal_tz - timedelta(days=1)
            
        target_local_dt = signal_dt_in_signal_tz.astimezone(LOCAL_TIMEZONE)
    except Exception as e:
        logger.error(f"Error parsing or converting signal entry time '{signal_entry_time_str}': {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid signal entry time format: {e}")

    logger.info(f"Signal entry time (GMT-4): {signal_entry_time_str}. Calculated local target entry time: {target_local_dt.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    # Allow a small buffer for late signals, e.g., up to 5 seconds past target entry time.
    if current_local_dt > target_local_dt + timedelta(seconds=5):
        logger.warning(f"Signal for {signal_asset} {signal_direction.value} (Entry: {signal_entry_time_str}) arrived late. "
                       f"Current local time: {current_local_dt.strftime('%H:%M:%S')}, Target local time: {target_local_dt.strftime('%H:%M:%S')}. "
                       f"Skipping trade.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "skipped", "message": "Signal arrived too late, trade skipped."})
    latency_mean = float(0)
    latency_sum = float(0)
    for _ in range(10):
        print(f"== api-eu.po.market ==")
        res = measure_one("demo-api-eu.po.market")
        dns = res.get("dns_ms")
        print(f"  DNS: {dns:.1f} ms")
        latency_sum += float(dns) #type: ignore
        if _ < 10:
            latency_mean = latency_sum / (_ + 1)
        await asyncio.sleep(1)
    
    time_to_wait_seconds = (target_local_dt - (datetime.now(LOCAL_TIMEZONE)- timedelta(milliseconds=latency_mean))).total_seconds()

    logger.info(f"New signal received. Initiating a new trade sequence for {signal_asset} {signal_direction.value}. Initial Amount: ${INITIAL_TRADE_AMOUNT:.2f}")
    
    # Set the global flag to indicate a sequence is active
    is_processing_trade_sequence = True

    if time_to_wait_seconds > 0:
        logger.info(f"Waiting {time_to_wait_seconds:.2f} seconds until target entry time: {target_local_dt.strftime('%H:%M:%S')}")
        await asyncio.sleep(time_to_wait_seconds)
        logger.info(f"Reached target entry time. Proceeding with trade for {signal_asset} {signal_direction.value}.")
    else:
        logger.info(f"Signal arrived exactly at or slightly past target entry time ({current_local_dt.strftime('%H:%M:%S')} vs {target_local_dt.strftime('%H:%M:%S')}). Placing trade immediately.")

    try:
        balance_before_trade = await pocket_option_client.get_balance() # type: ignore
        logger.info(f"Balance BEFORE initial trade: {balance_before_trade.balance} {balance_before_trade.currency}")
    except Exception as e:
        logger.warning(f"Could not retrieve balance before initial trade: {e}")

    trade_sequence_state.update({
        "active": True, # This "active" in state is for internal Martingale logic within the sequence
        "asset": signal_asset,
        "direction": signal_direction,
        "current_level": 0,
        "current_amount": INITIAL_TRADE_AMOUNT,
        "last_trade_id": None,
        "last_trade_status": "pending",
        "last_trade_open_time": None,
        "current_balance":None # type: ignore
    })

    try:
        order = await pocket_option_client.place_order( # type: ignore
            asset=trade_sequence_state["asset"],
            amount=trade_sequence_state["current_amount"],
            direction=trade_sequence_state["direction"],
            duration=trade_duration
        )
        entry_time = datetime.now(LOCAL_TIMEZONE)
        
        latency =   measure_one(" demo-api-eu.po.market")
        logger.info(f"latency: {latency}")
        # entry_time = datetime.now(LOCAL_TIMEZONE) + timedelta(milliseconds=float(latency["dns_ms"] if latency and "dns_ms" in latency else 0))
        logger.info(f"line 206: Initial trade placed successfully! Order ID: {order.order_id}, Status: {order.status}")
        trade_sequence_state["last_trade_id"] = order.order_id
        
        # Immediately try to get the open price/time for this trade
        # This is crucial for the candle-based Martingale decision
        initial_order_details: Optional[OrderResult] = None
        for i in range(3): # Try a few times to get initial order details
            try:
                initial_order_details = await pocket_option_client.check_order_result(order.order_id) # type: ignore
                logger.info(f"initial order details: {initial_order_details}")
                if initial_order_details and initial_order_details.amount: # type: ignore
                    trade_sequence_state["last_trade_open_price"] = initial_order_details.amount # type: ignore
                    trade_sequence_state["last_trade_open_time"] = initial_order_details.placed_at # type: ignore
                    logger.info(f"Initial trade open price obtained: {initial_order_details.amount} at {initial_order_details.placed_at}") # type: ignore
                    break
            except Exception as e:
                logger.warning(f"Could not get initial trade details (attempt {i+1}/3): {e}")
            await asyncio.sleep(0.05) # Small delay before retry

        if not trade_sequence_state["last_trade_open_price"]:
            logger.error(f"Failed to obtain open price for initial trade ID {order.amount}. This will affect Martingale decisions based on candles.")
            # Decide if you want to abort here or proceed with a potential risk.
            # For now, we'll proceed, but it's a critical warning.

        logger.info(f"Trade placed. Now initiating outcome monitoring for trade ID: {trade_sequence_state['last_trade_id']}")
        current_balance = await pocket_option_client.get_balance() # type: ignore
        print(current_balance)
        trade_sequence_state["current_balance"]= current_balance.balance
        logger.info(f"Current balance after placing initial trade for trade sequence: {trade_sequence_state['current_balance']}")
        asyncio.create_task(
            handle_trade_outcome_and_martingale(
                trade_sequence_state["last_trade_id"],
                trade_duration,
                trade_sequence_state["asset"],
                trade_sequence_state["direction"],
                trade_sequence_state["current_amount"],
                trade_sequence_state["current_balance"],
                entry_time
            )
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "initial_trade_placed",
            "message": "Initial trade placed successfully. Outcome will be processed shortly.",
            "trade_id": trade_sequence_state["last_trade_id"],
            "asset": trade_sequence_state["asset"],
            "direction": trade_sequence_state["direction"].value,
            "amount": trade_sequence_state["current_amount"],
            "martingale_level": trade_sequence_state["current_level"], # 0 for initial
            "current_balance":trade_sequence_state["current_balance"],
            "last_trade_status":trade_sequence_state["last_trade_status"]
        })
    except Exception as e:
        logger.error(f"Failed to place initial trade: {e}", exc_info=True)
        # Reset sequence and global flag on failure to place initial trade
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        trade_sequence_state["last_trade_open_price"] = None
        trade_sequence_state["last_trade_open_time"] = None
        trade_sequence_state["last_trade_status"] = None
        is_processing_trade_sequence = False # Release the lock
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to place initial trade: {e}")


async def connect_pocket_option_client() -> bool:
    global pocket_option_client, is_demo_session

    if pocket_option_client and pocket_option_client.is_connected:
        logger.info("Pocket Option client is already connected.")
        return True

    ssid = os.getenv('SSID')
    uid = os.getenv('UID') # UID checked, but not directly used for connection here
    
    if not ssid:
        logger.critical("SSID not found in .env. Cannot connect.")
        return False
    if not uid:
        logger.critical("UID not found in .env. Cannot connect.")
        return False

    if is_demo_session is None:
        logger.critical("Account type (DEMO/REAL) not set during startup. Cannot connect.")
        return False

    try:
        if not pocket_option_client:
            pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session,enable_logging=False)
            
        await pocket_option_client.connect()
        logger.info("Pocket Option client re-connected successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to re-connect Pocket Option client: {e}", exc_info=True)
        pocket_option_client = None
        return False

def save_to_env(key: str, value: str):
    """
    Saves or updates a key-value pair in the .env file.
    If the key already exists, its value is updated. Otherwise, the new key-value pair is added.
    Ensures value is enclosed in single quotes.
    """
    env_path = os.path.join(os.getcwd(), ".env")
    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}='{value}'\n") # Use single quotes
                    found = True
                else:
                    lines.append(line)

    if not found:
        lines.append(f"{key}='{value}'\n") # Use single quotes

    with open(env_path, "w") as f:
        f.writelines(lines)
    logger.info(f"Successfully saved {key} to .env file.")

async def handle_trade_outcome_and_martingale(trade_id: int|str, duration: int, asset: str, direction: OrderDirection, amount: float,after_entry_balance:float,entry_time:datetime) -> None:
    
    global trade_sequence_state, pocket_option_client, is_processing_trade_sequence
    logger.info(f"Monitoring trade ID: {trade_id} (Asset: {asset}, Direction: {direction.value}, Amount: ${amount:.2f}). Preparing for candle-based Martingale decision...")
    
    # Calculate time to wait until 5 seconds before trade ends
    # time_to_wait_seconds = (target_local_dt - datetime.now(LOCAL_TIMEZONE)).total_seconds()
    trade_end_time = (((entry_time+timedelta(seconds=duration))-(datetime.now(LOCAL_TIMEZONE))) + timedelta(seconds=0.05)).total_seconds()
    logger.info(f"Trade ID {trade_id} will end in approximately {trade_end_time:.2f} seconds.")
    if trade_end_time > 0:
        logger.info(f"Waiting {trade_end_time:.2f} seconds before checking candle for Martingale decision for trade ID {trade_id}.")
        await asyncio.sleep(trade_end_time)
    
    # --- Martingale Decision based on Candle ---
    martingale_reentry_needed = False
    try:
        for i in range(3): # Try a few times to get candle data
            try:
                current_balance = await pocket_option_client.get_balance() # type: ignore
                if current_balance.balance is not None and current_balance.balance > 0:
                    if i == 3  or current_balance.balance != after_entry_balance:
                        break
            except Exception as e:
                logger.warning(f"Could not retrieve balance before Martingale decision (attempt {i+1}/3): {e}")
                await asyncio.sleep(0.05) # Small delay before retry
        if current_balance.balance > after_entry_balance:
            logger.info(f"Balance check before Martingale decision: \nCurrent balance {current_balance.balance} {current_balance.currency} is more than balance after entry {after_entry_balance}. No Martingale needed.")
            martingale_reentry_needed = False
        else:
            logger.info(f"determine loss based on non-increased balance: Current balance {current_balance.balance} {current_balance.currency} is less or equal to the balance after entry {after_entry_balance}. Considering Martingale re-entry.")
            martingale_reentry_needed = True
    except Exception as e:
        logger.warning(f"Could not retrieve balance before Martingale decision: {e}. Aborting re-entry decision.")
        martingale_reentry_needed = False
    # Wait for the remaining 5 seconds until trade officially ends
    # logger.info(f"Waiting for remaining 5 seconds before potential Martingale re-entry for trade ID {trade_id}.")
    # await asyncio.sleep(5) 

    # --- Martingale Re-entry Logic ---
    if martingale_reentry_needed:
        logger.info(f"Predicted LOSS for Trade ID {trade_id}. Checking Martingale level...")
        if trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
            trade_sequence_state["current_level"] += 1
            trade_sequence_state["current_amount"] *= MARTINGALE_MULTIPLIER
            
            logger.info(f"Proceeding with Martingale Level {trade_sequence_state['current_level']} for {asset} {direction.value}. New Amount: ${trade_sequence_state['current_amount']:.2f}")
            try:
                next_order = await pocket_option_client.place_order( # type: ignore
                    asset=trade_sequence_state["asset"],
                    amount=trade_sequence_state["current_amount"],
                    direction=trade_sequence_state["direction"],
                    duration=duration
                )
                entry_time = datetime.now(LOCAL_TIMEZONE)
                logger.info(f"Martingale Level {trade_sequence_state['current_level']} trade placed successfully! Order ID: {next_order.order_id}, Status: {next_order.status}")
                trade_sequence_state["last_trade_id"] = next_order.order_id
                
                # Get the open price for the new Martingale trade
                martingale_order_details: Optional[OrderResult] = None
                for i in range(3): # Try a few times to get new order details
                    try:
                        martingale_order_details = await pocket_option_client.check_order_result(next_order.order_id) # type: ignore
                        if martingale_order_details and martingale_order_details.amount: # type: ignore
                            trade_sequence_state["last_trade_open_price"] = martingale_order_details.amount
                            trade_sequence_state["last_trade_open_time"] = martingale_order_details.placed_at # type: ignore
                            logger.info(f"Martingale trade open price obtained: {martingale_order_details.amount} at {martingale_order_details.placed_at}") # type: ignore
                            break
                    except Exception as e:
                        logger.warning(f"Could not get Martingale trade details (attempt {i+1}/3): {e}")
                    await asyncio.sleep(0.05) # Small delay before retry
                logger.info(f"Martingale trade placed. Now monitoring outcome for trade ID: {trade_sequence_state['last_trade_id']}")
                
            
                for i in range(3): # Try a few times to get candle data
                    try:
                        current_balance = await pocket_option_client.get_balance() # type: ignore
                        if current_balance.balance is not None and current_balance.balance > 0:
                            logger.info(f"Retrieved current balance after placing Martingale trade: {current_balance.balance} {current_balance.currency}")
                            trade_sequence_state["current_balance"]= current_balance.balance
                            break
                    except Exception as e:
                        await asyncio.sleep(0.05) # Small delay before retry
                        logger.warning(f"Could not retrieve balance after placing Martingale trade: {e} -->this affects next Martingale decision",exc_info=True)                    

                # Continue monitoring this new Martingale trade
                asyncio.create_task(
                    handle_trade_outcome_and_martingale(
                        trade_sequence_state["last_trade_id"],
                        duration,
                        trade_sequence_state["asset"],
                        trade_sequence_state["direction"],
                        trade_sequence_state["current_amount"],
                        trade_sequence_state["current_balance"],
                        entry_time
                    )
                )
            except Exception as e:
                logger.error(f"Failed to place Martingale Level {trade_sequence_state['current_level']} trade: {e}", exc_info=True)
                # FATAL: Reset sequence and global flag on failure to place Martingale trade
                logger.error(f"FATAL: Failed to place Martingale trade. Resetting entire sequence and releasing lock.")
                trade_sequence_state["active"] = False
                trade_sequence_state["current_level"] = 0
                trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
                trade_sequence_state["last_trade_open_price"] = None
                is_processing_trade_sequence = False # Release the lock
        else:
            logger.info(f"Trade LOSS for {asset} {direction.value} ${amount} at final Martingale level ({MAX_MARTINGALE_LEVELS}). Resetting sequence. Waiting for next signal.")
            # Reset sequence and global flag if max levels reached
            trade_sequence_state["active"] = False
            trade_sequence_state["current_level"] = 0
            trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
            trade_sequence_state["last_trade_open_price"] = None
            trade_sequence_state["last_trade_status"] = "Loss"
            is_processing_trade_sequence = False # Release the lock
    else: # predicted WIN or TIE
        logger.info(f"Predicted WIN/TIE for Trade ID {trade_id}. Resetting Martingale sequence. No re-entry.")
        # Always reset sequence and global flag on a predicted win/tie
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        trade_sequence_state["last_trade_open_price"] = None

        trade_sequence_state["last_trade_status"] = "win"
        is_processing_trade_sequence = False # Release the lock
    
    # --- Final Official Outcome Check for Logging (optional, not for Martingale decision) ---
    # We still check the official outcome for logging purposes, but the Martingale decision is already made.
    
    # Give it a small buffer after the trade is supposed to end for the official result to settle
    await asyncio.sleep(0.05) 
    try:
        save_to_env("TRADE_SEQUENCE_STATE", json.dumps(trade_sequence_state, indent=4) + "\n",)
        bot_settings = {"FIXED_TRADE_DURATION_SECONDS": os.getenv("FIXED_TRADE_DURATION_SECONDS", 300),
                        "INITIAL_TRADE_AMOUNT": os.getenv("INITIAL_TRADE_AMOUNT", 1.0),
                        "MARTINGALE_MULTIPLIER": os.getenv("MARTINGALE_MULTIPLIER", 2.0),
                        "MAX_MARTINGALE_LEVELS": os.getenv("MAX_MARTINGALE_LEVELS", 2),
                        }
        save_to_env(json.dumps("BOT_SETTINGS", indent=4), json.dumps(bot_settings, indent=4) + "\n",)
        logger.info(f"\n\n Checking official final outcome for Trade placed at {trade_sequence_state["last_trade_open_time"]}\n amount: {trade_sequence_state['current_amount']}\n asset: {asset}\n direction: {direction.value}\n\n")
    except Exception as e:
        logger.warning(f"Error saving trade sequence state to .env: {e}")
    if pocket_option_client and pocket_option_client.is_connected:
        try:
            if trade_sequence_state["last_trade_status"] == "win":
                profit = (await pocket_option_client.get_balance()).balance - after_entry_balance
                logger.info(f"\n\nOFFICIAL FINAL OUTCOME for Trade ID {trade_id}: \n Status:{trade_sequence_state["last_trade_status"].upper()} \nProfit: {profit:2f}) USD.\n\n")
            else:
                logger.info(f"OFFICIAL FINAL OUTCOME for Trade ID {trade_id}: {trade_sequence_state['last_trade_status'].upper()}.")
            trade_sequence_state["last_trade_open_time"] = None
        except Exception as e:
            logger.warning(f"Error checking official final trade result for ID {trade_id}: {e}")
    else:
        logger.warning(f"Pocket Option client not connected for official outcome check of trade ID {trade_id}.")

    trade_sequence_state["last_trade_status"] = None # Update state with actual outcome

    logger.info(f"Martingale Sequence State AFTER processing Trade ID {trade_id}: Active={trade_sequence_state['active']}, Level={trade_sequence_state['current_level']}, Amount={trade_sequence_state['current_amount']:.2f}, Global Processing Lock: {is_processing_trade_sequence}")
    
