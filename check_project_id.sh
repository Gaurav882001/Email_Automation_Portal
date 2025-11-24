#!/bin/bash

# Script to check the actual Google Cloud project ID
# Project NAME (display name) can be different from project ID

echo "=========================================="
echo "🔍 Checking Google Cloud Project Configuration"
echo "=========================================="
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "⚠️  gcloud CLI is not installed."
    echo "   Install from: https://cloud.google.com/sdk/docs/install"
    echo ""
    echo "   OR check manually:"
    echo "   1. Go to: https://console.cloud.google.com"
    echo "   2. Look at the project selector (top bar)"
    echo "   3. The PROJECT ID (not name) is what you need"
    echo "   4. It's usually lowercase, like: automationspecialist"
    exit 1
fi

# Get current project
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null)
echo "Current gcloud project: $CURRENT_PROJECT"
echo ""

# List all projects
echo "All projects you have access to:"
echo "----------------------------------------"
gcloud projects list --format="table(projectId,name)" 2>/dev/null | head -20
echo ""

# Check if automationspecialist exists
if gcloud projects describe automationspecialist > /dev/null 2>&1; then
    PROJECT_NAME=$(gcloud projects describe automationspecialist --format="value(name)" 2>/dev/null)
    echo "✅ Project ID 'automationspecialist' EXISTS"
    echo "   Project Name (display): $PROJECT_NAME"
    echo "   Project ID (for API): automationspecialist"
    echo ""
    echo "✓ This is the correct project ID to use in code!"
else
    echo "❌ Project ID 'automationspecialist' NOT FOUND"
    echo ""
    echo "Please check:"
    echo "  1. The project ID might be different"
    echo "  2. You might not have access to it"
    echo "  3. Check the list above for the correct project ID"
fi

echo ""
echo "=========================================="
echo "📝 IMPORTANT:"
echo "=========================================="
echo "• Project NAME (display): Can be 'AutomationSpecialist'"
echo "• Project ID (for API): Must be lowercase 'automationspecialist'"
echo "• In your .env file, use: GOOGLE_CLOUD_PROJECT_ID=automationspecialist"
echo "• When adding Pub/Sub permissions, use the PROJECT ID (lowercase)"
echo ""

