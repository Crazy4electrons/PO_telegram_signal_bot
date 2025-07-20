# Get the path to requirements.txt
$requirementsPath = "requirements.txt"

# Check if requirements.txt exists
if (Test-Path $requirementsPath) {
    Write-Host "Installing packages from requirements.txt..."
    pip install -r $requirementsPath
} else {
    Write-Host "requirements.txt not found. Please ensure the file exists in the current directory."
}