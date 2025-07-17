# main_asgi_app.py
from uvicorn.middleware.wsgi import WSGIMiddleware
from trader import app as flask_app # Import the Flask app instance from trader.py
import asyncio
import logging

logger = logging.getLogger(__name__)

# This will hold the PocketOption API client instance
# We'll initialize it once and reuse it.
global_pocket_option_api = None

async def initialize_pocket_option_client():
    """
    Initializes the PocketOption API client once for the application lifespan.
    """
    global global_pocket_option_api
    from trader import read_ssid_from_file, DEFAULT_ACCOUNT_TYPE # Import from trader.py

    # Check if already connected and connection is valid.
    # The pocketoptionapi_async library has a check_connection method.
    if global_pocket_option_api and global_pocket_option_api.check_connection():
        logger.info("PocketOption API client already initialized and connected.")
        return True

    ssid = read_ssid_from_file()
    if not ssid:
        logger.critical("Failed to get SSID during application startup. Trading will not work.")
        return False

    logger.info("Initializing PocketOption API client during startup.")
    from pocketoptionapi_async import AsyncPocketOptionClient
    global_pocket_option_api = AsyncPocketOptionClient(ssid, is_demo=(DEFAULT_ACCOUNT_TYPE == "PRACTICE"))

    try:
        connected = await global_pocket_option_api.connect()
        if not connected:
            logger.critical("PocketOption API initial connection failed during startup. Check logs.")
            global_pocket_option_api = None
            return False
        logger.info("PocketOption API successfully connected during startup.")
        await global_pocket_option_api.set_act(DEFAULT_ACCOUNT_TYPE)
        balance = await global_pocket_option_api.get_balance()
        logger.info(f"Initial {DEFAULT_ACCOUNT_TYPE} Balance: {balance}")
        return True
    except Exception as e:
        logger.critical(f"Critical error during PocketOption API startup connection: {e}", exc_info=True)
        global_pocket_option_api = None
        return False

# Use before_serving for startup logic (Flask 2.3+). This runs once before the server starts accepting requests.
@flask_app.before_serving
async def startup_logic():
    """Runs once before the server starts accepting requests."""
    logger.info("Running Flask before_serving logic for PocketOption API initialization.")
    success = await initialize_pocket_option_client()
    if not success:
        logger.critical("Failed to initialize PocketOption API client on startup.")

# Wrap the Flask (WSGI) application with Uvicorn's WSGIMiddleware
app = WSGIMiddleware(flask_app)