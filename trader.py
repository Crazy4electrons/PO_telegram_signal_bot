import os
import json
import time
import asyncio
import logging
import pytz
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from typing import Optional, AsyncIterator, Any
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection
from pocketoptionapi_async.models import OrderResult # Only import what's directly used

from parse_data import parse_macrodroid_trade_data

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

pocket_option_client: Optional[AsyncPocketOptionClient] = None
is_demo_session: Optional[bool] = None

FIXED_TRADE_DURATION_SECONDS = 300
INITIAL_TRADE_AMOUNT = 1.0
MARTINGALE_MULTIPLIER = 2.0
MAX_MARTINGALE_LEVELS = 2

SIGNAL_TIMEZONE = pytz.timezone('America/New_York')
LOCAL_TIMEZONE = pytz.timezone('Africa/Windhoek')

trade_sequence_state = {
    "active": False,
    "asset": None,
    "direction": None,
    "current_level": 0,
    "current_amount": INITIAL_TRADE_AMOUNT,
    "last_trade_id": None,
    "last_trade_status": None,
    # Removed open_price and open_time from here as they are not available immediately
    # and are retrieved when checking order result.
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
        logger.critical("SSID not found in .env. Please run scraper.py first to obtain it.")
        yield
        return
    if not uid:
        logger.critical("UID not found in .env. Please run scraper.py first to obtain it.")
        yield
        return

    pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session)

    try:
        await pocket_option_client.connect()
        logger.info("Pocket Option client connected successfully on startup.")
        
        balance = await pocket_option_client.get_balance()
        logger.info(f'Startup Balance: {balance.balance} {balance.currency} (Is Demo: {balance.is_demo})')

    except Exception as e:
        logger.error(f"Initial Pocket Option client connection failed on startup: {e}", exc_info=True)
        pocket_option_client = None
    
    yield

    logger.info("FastAPI lifespan shutdown event: Disconnecting Pocket Option client.")
    if pocket_option_client:
        await pocket_option_client.disconnect()
        logger.info("Pocket Option client disconnected during shutdown.")

app = FastAPI(lifespan=lifespan)

@app.post('/trade_signal')
async def trade_signal_webhook(request: Request) -> JSONResponse:
    global trade_sequence_state, pocket_option_client, is_demo_session

    if not pocket_option_client or not pocket_option_client.is_connected:
        logger.error("Pocket Option client is not connected. Cannot process trade signal.")
        if await connect_pocket_option_client():
             logger.info("Re-established Pocket Option connection for trade signal.")
        else:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Pocket Option API not connected.")

    raw_notification_text = (await request.body()).decode('utf-8')
    logger.info(f"Received raw notification from Macrodroid:\n{raw_notification_text}")

    parsed_data = parse_macrodroid_trade_data(raw_notification_text)

    if not parsed_data:
        logger.error("Failed to parse trade data from notification. Aborting trade attempt.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to parse trade data from notification.")

    signal_asset = parsed_data.get("asset_name_for_po")
    signal_direction_str = parsed_data.get("direction")
    signal_entry_time_str = parsed_data.get("entryTime")
    trade_duration = parsed_data.get("duration", FIXED_TRADE_DURATION_SECONDS)

    try:
        signal_direction = OrderDirection[signal_direction_str.upper()]
    except (KeyError, AttributeError):
        logger.error(f"Invalid or missing trade direction received: '{signal_direction_str}'. Must be 'CALL' or 'PUT'.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid trade direction.")

    if not signal_entry_time_str:
        logger.error("Signal is missing 'Entry at HH:MM'. Cannot determine precise entry time. Aborting.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Signal missing entry time.")
    if not signal_asset:
        logger.error("Signal is missing asset. Aborting trade.")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Signal missing asset.")

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
        logger.warning(f"Signal for {signal_asset} {signal_direction.value} (Entry: {signal_entry_time_str}) arrived late. Current local time: {current_local_dt.strftime('%H:%M:%S')}, Target local time: {target_local_dt.strftime('%H:%M:%S')}. Skipping trade.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "skipped", "message": "Signal arrived too late, trade skipped."})

    time_to_wait_seconds = (target_local_dt - current_local_dt).total_seconds()

    if trade_sequence_state["active"] and \
       trade_sequence_state["asset"] == signal_asset and \
       trade_sequence_state["direction"] == signal_direction and \
       trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
        
        logger.info(f"Ignoring new signal for {signal_asset} {signal_direction.value}."
                    f" An active Martingale sequence is already in progress (Level {trade_sequence_state['current_level']+1})."
                    f" Waiting for its outcome to decide next step or for a new, different signal.")
        return JSONResponse(status_code=status.HTTP_200_OK, content={
            "status": "ignored",
            "message": "Signal ignored. Active Martingale sequence in progress for this asset/direction."
        })
    elif trade_sequence_state["active"] and \
         (trade_sequence_state["asset"] != signal_asset or trade_sequence_state["direction"] != signal_direction):
        logger.warning(f"New signal for {signal_asset} {signal_direction.value} received while another sequence "
                       f"({trade_sequence_state['asset']} {trade_sequence_state['direction'].value}) was active. "
                       f"Resetting previous sequence and starting new one.")
        trade_sequence_state["active"] = False

    logger.info(f"New signal received. Starting a new trade sequence for {signal_asset} {signal_direction.value}. Initial Amount: ${INITIAL_TRADE_AMOUNT:.2f}")
    trade_sequence_state.update({
        "active": True,
        "asset": signal_asset,
        "direction": signal_direction,
        "current_level": 0,
        "current_amount": INITIAL_TRADE_AMOUNT,
        "last_trade_id": None,
        "last_trade_status": "pending",
    })

    if time_to_wait_seconds > 0:
        logger.info(f"Waiting {time_to_wait_seconds:.2f} seconds until target entry time: {target_local_dt.strftime('%H:%M:%S')}")
        await asyncio.sleep(time_to_wait_seconds)
        logger.info(f"Reached target entry time. Proceeding with trade for {signal_asset} {signal_direction.value}.")
    else:
        logger.info(f"Signal arrived exactly at or slightly past target entry time ({current_local_dt.strftime('%H:%M:%S')} vs {target_local_dt.strftime('%H:%M:%S')}). Placing trade immediately.")

    try:
        balance_before_trade = await pocket_option_client.get_balance()
        logger.info(f"Balance BEFORE initial trade: {balance_before_trade.balance} {balance_before_trade.currency}")
    except Exception as e:
        logger.warning(f"Could not retrieve balance before initial trade: {e}")

    try:
        order = await pocket_option_client.place_order(
            asset=trade_sequence_state["asset"],
            amount=trade_sequence_state["current_amount"],
            direction=trade_sequence_state["direction"],
            duration=trade_duration
        )
        logger.info(f"Initial trade placed successfully! Order ID: {order.order_id}, Status: {order.status}")
        trade_sequence_state["last_trade_id"] = order.order_id
        
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
            "status": "initial_trade_placed",
            "message": "Initial trade placed successfully. Outcome will be processed shortly.",
            "trade_id": trade_sequence_state["last_trade_id"],
            "asset": trade_sequence_state["asset"],
            "direction": trade_sequence_state["direction"].value,
            "amount": trade_sequence_state["current_amount"],
            "martingale_level": trade_sequence_state["current_level"] + 1
        })
    except Exception as e:
        logger.error(f"Failed to place initial trade: {e}", exc_info=True)
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to place initial trade: {e}")


async def connect_pocket_option_client() -> bool:
    global pocket_option_client, is_demo_session

    if pocket_option_client and pocket_option_client.is_connected:
        logger.info("Pocket Option client is already connected.")
        return True

    ssid = os.getenv('SSID')
    uid = os.getenv('UID')
    
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
            pocket_option_client = AsyncPocketOptionClient(ssid, is_demo=is_demo_session)
        
        await pocket_option_client.connect()
        logger.info("Pocket Option client re-connected successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to re-connect Pocket Option client: {e}", exc_info=True)
        pocket_option_client = None
        return False


async def handle_trade_outcome_and_martingale(trade_id: int, duration: int, asset: str, direction: OrderDirection, amount: float) -> None:
    global trade_sequence_state, pocket_option_client

    logger.info(f"Monitoring trade ID: {trade_id}. Waiting for trade to expire and result to be available...")
    
    # Wait for the trade duration plus a buffer for the result to come in.
    # The "5 seconds before end" check will now be part of the final result check,
    # as open_price is only available from check_order_result.
    await asyncio.sleep(duration + 5) # Wait for trade to end + 5s buffer for result propagation

    outcome = "loss"
    trade_details: Optional[OrderResult] = None

    if not pocket_option_client or not pocket_option_client.is_connected:
        logger.warning(f"Pocket Option client not connected for final check of trade ID {trade_id}. Assuming loss for Martingale progression.")
    else:
        try:
            trade_details = await pocket_option_client.check_order_result(trade_id)
            if trade_details:
                logger.info(f"Official final outcome for trade ID {trade_id}: Status={trade_details.status}, Profit={trade_details.profit}, Open Price: {trade_details.open_price}, Close Price: {trade_details.close_price}")
                if trade_details.status == "win":
                    outcome = "win"
                elif trade_details.status == "lose":
                    outcome = "loss"
                else:
                    logger.warning(f"Unexpected final trade result status: {trade_details.status}. Treating as loss for Martingale.")
                    outcome = "loss"
            else:
                logger.warning(f"Could not retrieve final result for trade ID {trade_id}. Treating as loss for Martingale.")
                outcome = "loss"
        except Exception as e:
            logger.error(f"Error checking final trade result for ID {trade_id}: {e}. Treating as loss for Martingale.", exc_info=True)
            outcome = "loss"

    trade_sequence_state["last_trade_status"] = outcome

    logger.info(f"Final outcome for trade ID {trade_id} ({asset} {direction.value} ${amount}): {outcome}")

    try:
        balance_after_trade = await pocket_option_client.get_balance()
        logger.info(f"Balance AFTER trade ID {trade_id}: {balance_after_trade.balance} {balance_after_trade.currency}")
    except Exception as e:
        logger.warning(f"Could not retrieve balance after trade ID {trade_id}: {e}")


    if outcome == "loss":
        if trade_sequence_state["current_level"] < MAX_MARTINGALE_LEVELS:
            trade_sequence_state["current_level"] += 1
            trade_sequence_state["current_amount"] *= MARTINGALE_MULTIPLIER
            
            logger.info(f"Trade LOSS for {asset} {direction.value} ${amount}. Proceeding with Martingale Level {trade_sequence_state['current_level']+1}. New Amount: ${trade_sequence_state['current_amount']:.2f}")

            try:
                balance_before_martingale = await pocket_option_client.get_balance()
                logger.info(f"Balance BEFORE Martingale Level {trade_sequence_state['current_level']+1} trade: {balance_before_martingale.balance} {balance_before_martingale.currency}")
            except Exception as e:
                logger.warning(f"Could not retrieve balance before Martingale trade: {e}")

            try:
                next_order = await pocket_option_client.place_order(
                    asset=trade_sequence_state["asset"],
                    amount=trade_sequence_state["current_amount"],
                    direction=trade_sequence_state["direction"],
                    duration=duration
                )
                logger.info(f"Martingale Level {trade_sequence_state['current_level']+1} trade placed successfully! Order ID: {next_order.order_id}, Status: {next_order.status}")
                trade_sequence_state["last_trade_id"] = next_order.order_id

                asyncio.create_task(
                    handle_trade_outcome_and_martingale(
                        trade_sequence_state["last_trade_id"],
                        duration,
                        trade_sequence_state["asset"],
                        trade_sequence_state["direction"],
                        trade_sequence_state["current_amount"]
                    )
                )
            except Exception as e:
                logger.error(f"Failed to place Martingale Level {trade_sequence_state['current_level']+1} trade: {e}", exc_info=True)
                trade_sequence_state["active"] = False
                trade_sequence_state["current_level"] = 0
                trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
        else:
            logger.info(f"Trade LOSS for {asset} {direction.value} ${amount} at final Martingale level ({MAX_MARTINGALE_LEVELS + 1}). Resetting sequence. Waiting for next signal.")
            trade_sequence_state["active"] = False
            trade_sequence_state["current_level"] = 0
            trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT
    else:
        logger.info(f"Trade WIN for {asset} {direction.value} ${amount}. Resetting Martingale sequence. Waiting for next signal.")
        trade_sequence_state["active"] = False
        trade_sequence_state["current_level"] = 0
        trade_sequence_state["current_amount"] = INITIAL_TRADE_AMOUNT

