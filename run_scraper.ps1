# --- REPLACE THESE PLACEHOLDERS WITH YOUR ACTUAL POCKET OPTION CREDENTIALS ---
$poEmail = "got1joke@gmail.com"         # <--- REPLACE THIS
$poPassword = "0nly!P@5s@PO"   # <--- REPLACE THIS
# -----------------------------------------------------------------------------

# Set environment variables for the current PowerShell session
$env:PO_EMAIL = $poEmail
$env:PO_PASSWORD = $poPassword
$env:SSID_REFRESH_INTERVAL_MINUTES = "86400" # Optional: Set refresh interval (default 30 mins)

# Navigate to the directory where your Python scripts are located
# Adjust this path if your scripts are not in the same directory as this .ps1 file
# Set-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Definition)

# Run the Python scraper script
# You might need to specify the full path to your python executable if it's not in your PATH
uv run scraper.py

# Optional: Clear environment variables after the script finishes (or after closing the terminal)
# Remove-Item Env:\PO_EMAIL
# Remove-Item Env:\PO_PASSWORD