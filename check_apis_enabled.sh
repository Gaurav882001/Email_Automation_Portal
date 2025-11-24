#!/bin/bash

# Script to check if required Google Cloud APIs are enabled
# Usage: ./check_apis_enabled.sh

PROJECT_ID="automationspecialist"

echo "=========================================="
echo "🔍 Checking Required Google Cloud APIs"
echo "=========================================="
echo "Project: $PROJECT_ID"
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "❌ ERROR: gcloud CLI is not installed."
    echo "   Install from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Set the project
gcloud config set project $PROJECT_ID > /dev/null 2>&1

echo "Checking enabled APIs..."
echo ""

# Check Pub/Sub API
echo "1. Cloud Pub/Sub API:"
if gcloud services list --enabled --filter="name:pubsub.googleapis.com" --format="value(name)" 2>/dev/null | grep -q "pubsub.googleapis.com"; then
    echo "   ✅ ENABLED"
else
    echo "   ❌ NOT ENABLED"
    echo "   → Enable it: https://console.cloud.google.com/apis/library/pubsub.googleapis.com?project=$PROJECT_ID"
    echo "   → Or run: gcloud services enable pubsub.googleapis.com --project=$PROJECT_ID"
fi

echo ""

# Check Gmail API
echo "2. Gmail API:"
if gcloud services list --enabled --filter="name:gmail.googleapis.com" --format="value(name)" 2>/dev/null | grep -q "gmail.googleapis.com"; then
    echo "   ✅ ENABLED"
else
    echo "   ❌ NOT ENABLED"
    echo "   → Enable it: https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=$PROJECT_ID"
    echo "   → Or run: gcloud services enable gmail.googleapis.com --project=$PROJECT_ID"
fi

echo ""

# Check Cloud Resource Manager API (needed for IAM)
echo "3. Cloud Resource Manager API:"
if gcloud services list --enabled --filter="name:cloudresourcemanager.googleapis.com" --format="value(name)" 2>/dev/null | grep -q "cloudresourcemanager.googleapis.com"; then
    echo "   ✅ ENABLED"
else
    echo "   ⚠️  NOT ENABLED (usually enabled by default)"
fi

echo ""
echo "=========================================="
echo "📋 Summary"
echo "=========================================="
echo "If any API is not enabled, enable it using the links above."
echo "After enabling, wait 2-3 minutes for the API to be fully activated."
echo ""

