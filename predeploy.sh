#!/bin/bash
# Pre-deploy gate — full test suite + check --deploy
#
# Phase 3.5 — run this on the deploy box BEFORE invoking deploy.sh.
# Either succeeds and you proceed, or fails and you fix what's broken.
#
# Usage:
#   bash /var/www/aspired/app/predeploy.sh
#
# Exit 0 on green, non-zero on any failure.

set -e
set -o pipefail

APP_DIR="/var/www/aspired/app"
VENV="/var/www/aspired/venv"
PYTHON="$VENV/bin/python"
LOG="/var/www/aspired/logs/predeploy.log"

echo "========================================" | tee -a "$LOG"
echo "Pre-deploy started: $(date)"               | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"

cd "$APP_DIR"

# Step 1 — Django system check, deployment mode.
# Validates SECURE_SSL_REDIRECT, HSTS, etc. are correct.
echo "[1/2] Running Django check --deploy..." | tee -a "$LOG"
"$PYTHON" manage.py check --deploy 2>&1 | tee -a "$LOG"

# Step 2 — Full test suite.
# Per CLAUDE.md: targeted app tests are the dev-loop default; the
# FULL suite runs HERE before prod deploy.
echo "[2/2] Running full test suite..." | tee -a "$LOG"
"$PYTHON" manage.py test 2>&1 | tee -a "$LOG"

echo "========================================" | tee -a "$LOG"
echo "✓ Pre-deploy gate PASSED — safe to deploy" | tee -a "$LOG"
echo "Pre-deploy completed: $(date)"           | tee -a "$LOG"
echo "========================================" | tee -a "$LOG"
