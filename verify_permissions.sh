#!/bin/bash

# Quick script to verify Pub/Sub permissions
# Usage: ./verify_permissions.sh gauravv882001@gmail.com

EMAIL="${1:-gauravv882001@gmail.com}"
PROJECT_ID="automationspecialist"
TOPIC_NAME="gmail-notifs"

echo "=========================================="
echo "Verifying Pub/Sub Permissions"
echo "=========================================="
echo "Email to check: $EMAIL"
echo "Project: $PROJECT_ID"
echo "Topic: $TOPIC_NAME"
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "⚠️  gcloud CLI is not installed."
    echo "   Install from: https://cloud.google.com/sdk/docs/install"
    echo ""
    echo "   OR check manually at:"
    echo "   https://console.cloud.google.com/cloudpubsub/topic/detail/$TOPIC_NAME?project=$PROJECT_ID"
    exit 1
fi

# Set the project
gcloud config set project $PROJECT_ID &> /dev/null

echo "Checking IAM policy for topic '$TOPIC_NAME'..."
echo ""

# Get IAM policy for the topic
POLICY=$(gcloud pubsub topics get-iam-policy $TOPIC_NAME --project=$PROJECT_ID 2>&1)

if [ $? -eq 0 ]; then
    echo "✓ Topic exists and IAM policy retrieved:"
    echo ""
    echo "$POLICY" | grep -A 5 "$EMAIL" || echo "❌ Email '$EMAIL' NOT FOUND in permissions!"
    echo ""
    
    # Check if email has publisher role
    if echo "$POLICY" | grep -q "$EMAIL.*pubsub.publisher"; then
        echo "✅ SUCCESS: $EMAIL has Pub/Sub Publisher permission!"
    else
        echo "❌ ERROR: $EMAIL does NOT have Pub/Sub Publisher permission"
        echo ""
        echo "To fix, run:"
        echo "  ./fix_pubsub_permissions.sh $EMAIL"
    fi
else
    echo "❌ Error: Could not retrieve IAM policy"
    echo "   Make sure:"
    echo "   1. Topic '$TOPIC_NAME' exists"
    echo "   2. You have permission to view IAM policies"
    echo "   3. Project ID '$PROJECT_ID' is correct"
fi

echo ""
echo "=========================================="

