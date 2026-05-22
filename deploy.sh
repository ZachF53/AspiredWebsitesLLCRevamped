#!/bin/bash
# Aspired Websites — Production Deploy Script
# Usage: bash /var/www/aspired/app/deploy.sh
# Run this on the server after every git push

set -e           # Exit immediately on any error
set -o pipefail  # ...including when the failing command is part of a pipe

APP_DIR="/var/www/aspired/app"
VENV="/var/www/aspired/venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
LOG="/var/www/aspired/logs/deploy.log"

echo "========================================" | tee -a $LOG
echo "Deploy started: $(date)" | tee -a $LOG
echo "========================================" | tee -a $LOG

# Step 1 — Pull latest code
echo "[1/7] Pulling latest code from GitHub..." | tee -a $LOG
cd $APP_DIR
git config --global safe.directory $APP_DIR
git pull origin main 2>&1 | tee -a $LOG

# Step 2 — Install/update dependencies
echo "[2/7] Installing dependencies..." | tee -a $LOG
$PIP install -r requirements.txt --quiet 2>&1 | tee -a $LOG

# Step 3 — Run migrations
echo "[3/7] Running migrations..." | tee -a $LOG
$PYTHON manage.py migrate --noinput 2>&1 | tee -a $LOG

# Step 4 — Collect static files
echo "[4/7] Collecting static files..." | tee -a $LOG
$PYTHON manage.py collectstatic --noinput --clear 2>&1 | tee -a $LOG

# Step 5 — Run Django system check
echo "[5/7] Running Django system check..." | tee -a $LOG
$PYTHON manage.py check --deploy 2>&1 | tee -a $LOG

# Step 6 — Restart services
echo "[6/7] Restarting services..." | tee -a $LOG
supervisorctl restart aspiredwebsites 2>&1 | tee -a $LOG
supervisorctl restart aspiredwebsites-celery 2>&1 | tee -a $LOG
supervisorctl restart aspiredwebsites-celerybeat 2>&1 | tee -a $LOG

# Step 7 — Verify everything is running
echo "[7/7] Verifying services..." | tee -a $LOG
supervisorctl status 2>&1 | tee -a $LOG

# Health check
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    https://aspiredwebsites.com)
echo "Site HTTP status: $HTTP_CODE" | tee -a $LOG

if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Deploy successful — site is live" | tee -a $LOG
else
    echo "✗ WARNING — site returned $HTTP_CODE" | tee -a $LOG
    echo "Check logs: tail -30 /var/www/aspired/logs/gunicorn-error.log"
    exit 1
fi

echo "Deploy completed: $(date)" | tee -a $LOG
echo "========================================" | tee -a $LOG
