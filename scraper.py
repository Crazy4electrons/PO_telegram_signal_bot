# scraper.py
import os
import time
import logging
import urllib.parse
import re
import json

from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

LOCAL_SHARED_DATA_DIR = "./shared_data"
SSID_FILE_PATH = os.path.join(LOCAL_SHARED_DATA_DIR, "ssid.txt")

MANUAL_EDGEDRIVER_PATH = r"C:\Users\IT-SUPPORT-03\Documents\letGo\drivers\msedgedriver.exe"
HARDCODED_UID = 92118257

def get_ssid_from_browser(email, password):
    logger.info("Starting fresh Edge browser instance to get SSID...")
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")

    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-features=RendererCodeIntegrity")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")

    service = Service(MANUAL_EDGEDRIVER_PATH)
    driver = None
    try:
        driver = webdriver.Edge(service=service, options=options)
        driver.get("https://pocketoption.com/en/login/")

        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.NAME, "password"))
        )

        email_field = driver.find_element(By.NAME, "email")
        password_field = driver.find_element(By.NAME, "password")
        login_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")

        email_field.send_keys(email)
        password_field.send_keys(password)
        login_button.click()

        WebDriverWait(driver, 60).until(
            EC.url_contains("cabinet") or EC.url_contains("dashboard") or \
            EC.presence_of_element_located((By.CSS_SELECTOR, ".header-user__name"))
        )
        logger.info("Successfully logged in to Pocket Option website.")

        cookies = driver.get_cookies()
        raw_ci_session_value = None
        for cookie in cookies:
            if cookie['name'] == 'ci_session':
                raw_ci_session_value = cookie['value']
                break

        if raw_ci_session_value:
            decoded_ci_session = urllib.parse.unquote(raw_ci_session_value)
            logger.info(f"Decoded ci_session: {decoded_ci_session}")

            extracted_session_id = None

            session_id_match = re.search(r's:10:"session_id";s:32:"([a-f0-9]{32})"', decoded_ci_session)
            if session_id_match:
                extracted_session_id = session_id_match.group(1)

            if extracted_session_id:
                logger.info(f"Extracted session_id: {extracted_session_id}, Using hardcoded UID: {HARDCODED_UID}")

                # --- CRITICAL FINAL CHANGE HERE ---
                # Add "isOptimized":true to match browser's SSID exactly
                full_ssid_string = f'42["auth",{{"session":"{extracted_session_id}","isDemo":1,"uid":{HARDCODED_UID},"platform":9,"isFastHistory":true}}]'
                logger.info(f"Constructed full SSID string: {full_ssid_string}")
                return full_ssid_string
            else:
                logger.error("Could not find 'session_id' within the decoded 'ci_session' cookie.")
                return None
        else:
            logger.error("'ci_session' cookie not found after login.")
            return None

    except Exception as e:
        logger.error(f"Error during SSID retrieval: {e}", exc_info=True)
        return None
    finally:
        pass

def write_ssid_to_file(ssid):
    try:
        os.makedirs(LOCAL_SHARED_DATA_DIR, exist_ok=True)
        with open(SSID_FILE_PATH, "w") as f:
            f.write(ssid)
        logger.info(f"SSID successfully written to {SSID_FILE_PATH}")
    except Exception as e:
        logger.error(f"Failed to write SSID to file {SSID_FILE_PATH}: {e}")

if __name__ == "__main__":
    email = os.getenv('PO_EMAIL')
    password = os.getenv('PO_PASSWORD')
    refresh_interval_minutes = int(os.getenv('SSID_REFRESH_INTERVAL_MINUTES', 30))
    refresh_interval_seconds = refresh_interval_minutes * 60

    if not email or not password:
        logger.error("PO_EMAIL and PO_PASSWORD environment variables must be set for the scraper.")
        logger.error("Please run this script like: PO_EMAIL='your@email.com' PO_PASSWORD='your_pass' python scraper.py")
        exit(1)

    while True:
        logger.info(f"Attempting to refresh SSID. Next refresh in {refresh_interval_minutes} minutes.")
        ssid = get_ssid_from_browser(email, password)
        if ssid:
            write_ssid_to_file(ssid)
            logger.info("SSID obtained. Browser window will remain open. Close this terminal to close browser.")
            time.sleep(refresh_interval_seconds)
        else:
            logger.warning("Failed to get SSID. Retrying after interval.")
            time.sleep(refresh_interval_seconds)