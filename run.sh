#!/bin/bash
cd "$(dirname "$0")"

# Check & Create Virtual Environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
fi
./venv/bin/pip install --requirement requirements.txt

echo "🚀 Starting Locket Gold V2 (Professional Edition)..."

# Kill existing instances
pkill -f "python3 bot.py" 2>/dev/null
pkill -f "main.py" 2>/dev/null
pkill -f "web_store.py" 2>/dev/null
sleep 1

# Run Web Store in background (sales front + admin panel)
nohup ./venv/bin/python3 web_store.py >> web_store.out 2>&1 &
echo "🛒 Web store started — see .env (WEB_HOST/WEB_PORT) for the address"

# Run Main
./venv/bin/python3 main.py
