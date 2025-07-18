# scraper_debug.py - Highly verbose scraper for debugging Pocket Option authentication

import os
import json
import time
import re
import logging
import urllib.parse
from typing import cast, List, Dict, Any

from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from dotenv import load_dotenv # Import dotenv

# Load environment variables from .env file
load_dotenv()

# --- MANUAL EDGE DRIVER PATH ---
# This path must point to your msedgedriver.exe
MANUAL_EDGEDRIVER_PATH = r"C:\Users\IT-SUPPORT-03\Documents\letGo\drivers\msedgedriver.exe"
# -------------------------------

# Configure logging for this script to provide clear, structured output.
# Set level to DEBUG to capture all possible information.
logging.basicConfig(
    level=logging.DEBUG, # <--- Set to DEBUG
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(name)s", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

LOCAL_SHARED_DATA_DIR = "./shared_data"
# SSID_FILE_PATH is not strictly used for reading/writing SSID in this version,
# as it's primarily handled by .env, but kept for consistency if needed.
SSID_FILE_PATH = os.path.join(LOCAL_SHARED_DATA_DIR, "ssid.txt") 

HARDCODED_UID = 92118257 # Ensure this is still your actual UID


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
                    lines.append(f"{key}='{value}'\n") # <--- Use single quotes
                    found = True
                else:
                    lines.append(line)

    if not found:
        lines.append(f"{key}='{value}'\n") # <--- Use single quotes

    with open(env_path, "w") as f:
        f.writelines(lines)
    logger.info(f"Successfully saved {key} to .env file.")


def get_pocketoption_ssid(email: str, password: str):
    """
    Automates the process of logging into PocketOption using Microsoft Edge,
    navigating to a specific cabinet page, and then scraping WebSocket traffic
    to extract the session ID (SSID).
    """
    logger.info("Starting Microsoft Edge browser instance for automated login...")
    
    edge_options = EdgeOptions()
    # No headless mode to allow visual debugging
    edge_options.add_argument("--no-sandbox")
    edge_options.add_argument("--disable-dev-shm-usage")
    edge_options.add_argument("--disable-gpu")
    edge_options.add_argument("--window-size=1920,1080")
    edge_options.add_argument("--start-maximized")
    edge_options.add_argument("--log-level=0") # Set Edge's internal logging to verbose
    edge_options.add_argument("--remote-debugging-port=9222")
    edge_options.add_argument("--disable-features=RendererCodeIntegrity")
    edge_options.add_argument("--disable-extensions")
    edge_options.add_argument("--disable-background-networking")

    # Enable performance logging for Edge (CRITICAL for capturing WebSocket traffic)
    edge_options.set_capability("ms:loggingPrefs", {"performance": "ALL"})

    driver = None
    try:
        service = Service(MANUAL_EDGEDRIVER_PATH)
        driver = webdriver.Edge(service=service, options=edge_options)
        logger.info("Microsoft Edge WebDriver initialized successfully.")

        login_url = "https://pocketoption.com/en/login/"
        cabinet_base_url = "https://pocketoption.com/en/cabinet"
        target_cabinet_url = "https://pocketoption.com/en/cabinet/demo-quick-high-low/"
        
        # Regex to capture the exact authentication message you provided.
        # This is very specific to the structure: 42["auth",{"session":"...","isDemo":0,"uid":92118257,"platform":2,"isFastHistory":true,"isOptimized":true}]
        # It handles escaped quotes within the session string.
        ssid_pattern = r'(42\["auth",\{"session":"((?:\\.|[^"\\])*)","isDemo":(?:true|false|\d+),"uid":\d+,"platform":\d+,"isFastHistory":(?:true|false),"isOptimized":(?:true|false)\}\])'
        
        logger.info(f"Navigating to login page: {login_url}")
        driver.get(login_url)

        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.NAME, "email")))
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.NAME, "password")))
        WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))

        email_field = driver.find_element(By.NAME, "email")
        password_field = driver.find_element(By.NAME, "password")
        login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")

        email_field.send_keys(email)
        password_field.send_keys(password)
        login_button.click()
        logger.info("Login credentials entered and login button clicked.")

        # Wait for successful login and redirection to cabinet/dashboard
        WebDriverWait(driver, 60).until(
            EC.url_contains("cabinet") or EC.url_contains("dashboard") or 
            EC.presence_of_element_located((By.CSS_SELECTOR, ".header-user__name"))
        )
        logger.info("Successfully logged in to Pocket Option website.")

        # Now navigate to the specific target URL within the cabinet to ensure all WebSocket connections are made.
        logger.info(f"Navigating to target cabinet page: {target_cabinet_url}")
        driver.get(target_cabinet_url)

        WebDriverWait(driver, 60).until(EC.url_contains(target_cabinet_url))
        logger.info("Successfully navigated to the target cabinet page.")

        # Give the page some time to load all WebSocket connections and messages.
        time.sleep(10) # Increased sleep to ensure more logs are captured

        performance_logs = cast(List[Dict[str, Any]], driver.get_log("performance"))
        logger.info(f"Collected {len(performance_logs)} performance log entries. Analyzing for SSID...")

        found_full_ssid_string = None
        # Iterate through the performance logs to find WebSocket frames.
        for i, entry in enumerate(performance_logs):
            try:
                message = json.loads(entry["message"])
                log_method = message["message"]["method"]
                
                # Log all WebSocket frame messages in detail
                if log_method == "Network.webSocketFrameReceived" or log_method == "Network.webSocketFrameSent":
                    payload_data = message["message"]["params"]["response"]["payloadData"]
                    logger.debug(f"--- WebSocket Frame ({log_method}) Entry {i} ---")
                    logger.debug(f"Timestamp: {entry['timestamp']}")
                    logger.debug(f"Frame Type: {'Received' if log_method == 'Network.webSocketFrameReceived' else 'Sent'}")
                    logger.debug(f"Payload Data: {payload_data}")
                    logger.debug("-----------------------------------")

                    # Attempt to find the full SSID string using the defined regex pattern.
                    match = re.search(ssid_pattern, payload_data)
                    if match:
                        found_full_ssid_string = match.group(1)
                        logger.info(
                            f"FOUND SSID IN LOGS: {found_full_ssid_string}"
                        )
                        # Do NOT break here. Continue logging other messages to see the full loop.
                        # We will save the *last* found valid SSID.

                # Optionally log other interesting network events
                elif "Network" in log_method:
                    logger.debug(f"Network Event ({log_method}): {message['message'].get('params', {}).get('request', {}).get('url', '')}")

            except json.JSONDecodeError:
                logger.debug(f"Skipping non-JSON log entry {i}.")
            except KeyError as ke:
                logger.debug(f"Skipping log entry {i} due to missing key: {ke}. Entry: {entry}")
            except Exception as e:
                logger.error(f"Error processing log entry {i}: {e}", exc_info=True)


        if found_full_ssid_string:
            save_to_env("SSID", found_full_ssid_string)
            logger.info("Full SSID string successfully extracted and saved to .env.")
            return found_full_ssid_string
        else:
            logger.warning(
                "Full SSID string pattern not found in WebSocket logs after login."
            )
            return None

    except Exception as e:
        logger.error(f"An error occurred during Edge automation: {e}", exc_info=True)
        return None
    finally:
        if driver:
            driver.quit()
            logger.info("WebDriver closed.")


if __name__ == "__main__":
    email = os.getenv('PO_EMAIL')
    password = os.getenv('PO_PASSWORD')
    refresh_interval_minutes = int(os.getenv('SSID_REFRESH_INTERVAL_MINUTES', 30))
    refresh_interval_seconds = refresh_interval_minutes * 60

    if not email or not password:
        logger.error("PO_EMAIL and PO_PASSWORD environment variables must be set for the scraper.")
        logger.error("Please ensure your run_scraper.ps1 script sets these variables before running scraper.py.")
        exit(1)

    # Ensure shared_data directory exists
    os.makedirs(LOCAL_SHARED_DATA_DIR, exist_ok=True)

    while True:
        logger.info(f"Attempting to refresh SSID. Next refresh in {refresh_interval_minutes} minutes.")
        get_pocketoption_ssid(email, password)
        logger.info("SSID extraction attempt completed.")
        
        logger.info(f"Waiting {refresh_interval_minutes} minutes before next SSID refresh attempt.")
        time.sleep(refresh_interval_seconds)