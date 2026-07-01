#!/usr/bin/env bash
# macOS automatic cron trigger setup for Market Report Bot
# This script configures local crontab to trigger your GitHub Actions workflow instantly.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_VENV="$PROJECT_DIR/venv/bin/python"
TRIGGER_SCRIPT="$PROJECT_DIR/scripts/trigger_workflow.py"
ENV_FILE="$PROJECT_DIR/.env"

echo "=== macOS Local Cron Trigger Setup ==="
echo "This script will add 4 scheduled trigger tasks to your macOS crontab."
echo "These tasks will trigger GitHub Actions workflows instantly via Webhook API,"
echo "bypassing the GHA queue delays (starting within 5-10 seconds)."
echo ""

# 1. Prompt for GitHub Personal Access Token (PAT)
if [ -f "$ENV_FILE" ] && grep -q "GITHUB_TOKEN=" "$ENV_FILE"; then
    CURRENT_TOKEN=$(grep "GITHUB_TOKEN=" "$ENV_FILE" | cut -d'=' -f2 | tr -d '"' | tr -d "'")
    echo "✓ Existing GITHUB_TOKEN detected in .env."
    read -p "Do you want to overwrite it? (y/N): " -r OVERWRITE
    if [[ "$OVERWRITE" =~ ^[Yy]$ ]]; then
        read -sp "Enter your GitHub Personal Access Token (repo scope): " GITHUB_TOKEN
        echo ""
    else
        GITHUB_TOKEN="$CURRENT_TOKEN"
    fi
else
    read -sp "Enter your GitHub Personal Access Token (repo scope): " GITHUB_TOKEN
    echo ""
fi

if [ -z "$GITHUB_TOKEN" ]; then
    echo "❌ Error: GitHub Token cannot be empty."
    exit 1
fi

# Write/Update .env file
mkdir -p "$(dirname "$ENV_FILE")"
# Remove existing GITHUB_TOKEN line
if [ -f "$ENV_FILE" ]; then
    grep -v "GITHUB_TOKEN=" "$ENV_FILE" > "${ENV_FILE}.tmp" || true
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
fi
echo "GITHUB_TOKEN=\"$GITHUB_TOKEN\"" >> "$ENV_FILE"
echo "✓ GITHUB_TOKEN saved safely to local .env file (git-ignored)."

# 2. Prepare Cron Jobs
# We direct output logs to PROJECT_DIR/cron_trigger.log for transparency and debugging
LOG_FILE="$PROJECT_DIR/cron_trigger.log"

CRON_TW_OPEN="30 8 * * 1-5 cd $PROJECT_DIR && $PYTHON_VENV $TRIGGER_SCRIPT tw_open >> $LOG_FILE 2>&1"
CRON_TW_CLOSE="30 15 * * 1-5 cd $PROJECT_DIR && $PYTHON_VENV $TRIGGER_SCRIPT tw_close >> $LOG_FILE 2>&1"
CRON_US_OPEN="0 21 * * 1-5 cd $PROJECT_DIR && $PYTHON_VENV $TRIGGER_SCRIPT us_open >> $LOG_FILE 2>&1"
CRON_US_CLOSE="30 8 * * 2-6 cd $PROJECT_DIR && $PYTHON_VENV $TRIGGER_SCRIPT us_close >> $LOG_FILE 2>&1"

# 3. Read current crontab, filter out old report-bot triggers, and append new ones
echo "Configuring macOS crontab ..."

TMP_CRON=$(mktemp)
crontab -l > "$TMP_CRON" 2>/dev/null || true

# Remove any existing market-report-bot triggers to avoid duplicates
grep -v "trigger_workflow.py" "$TMP_CRON" > "${TMP_CRON}.clean" || true

# Append new triggers
echo "" >> "${TMP_CRON}.clean"
echo "# --- Market Report Bot Trigger Jobs ---" >> "${TMP_CRON}.clean"
echo "$CRON_TW_OPEN" >> "${TMP_CRON}.clean"
echo "$CRON_TW_CLOSE" >> "${TMP_CRON}.clean"
echo "$CRON_US_OPEN" >> "${TMP_CRON}.clean"
echo "$CRON_US_CLOSE" >> "${TMP_CRON}.clean"
echo "# --------------------------------------" >> "${TMP_CRON}.clean"

# Apply new crontab
crontab "${TMP_CRON}.clean"

rm -f "$TMP_CRON" "${TMP_CRON}.clean"

echo "✓ macOS crontab updated successfully!"
echo "--------------------------------------------------------"
echo "Scheduled tasks added:"
echo "  1. 台股開盤戰報: 週一至週五 08:30 TPE"
echo "  2. 台股收盤日報: 週一至週五 15:30 TPE"
echo "  3. 美股開盤戰報: 週一至週五 21:00 TPE (為美股 21:30 開盤做準備)"
echo "  4. 美股收盤日報: 週二至週六 08:30 TPE"
echo "--------------------------------------------------------"
echo "Log file: $LOG_FILE"
echo "Setup complete! You can test it by running: python scripts/trigger_workflow.py us_open"
