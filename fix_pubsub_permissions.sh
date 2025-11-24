#!/bin/bash

# Script to fix Pub/Sub permissions for Gmail watch
# Usage: ./fix_pubsub_permissions.sh gauravv882001@gmail.com

EMAIL="${1:-gauravv882001@gmail.com}"
PROJECT_ID="automationspecialist"
TOPIC_NAME="gmail-notifs"

echo "=========================================="
echo "Fixing Pub/Sub Permissions for Gmail Watch"
echo "=========================================="
echo "Email: $EMAIL"
echo "Project: $PROJECT_ID"
echo "Topic: $TOPIC_NAME"
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI is not installed."
    echo "Please install it from: https://cloud.google.com/sdk/docs/install"
    echo ""
    echo "OR use the manual steps in VERIFY_PUBSUB_PERMISSIONS.md"
    exit 1
fi

# Check if user is authenticated
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q .; then
    echo "ERROR: Not authenticated with gcloud."
    echo "Please run: gcloud auth login"
    exit 1
fi

# Set the project
echo "Setting project to $PROJECT_ID..."
gcloud config set project $PROJECT_ID

# Check if topic exists
echo "Checking if topic exists..."
if ! gcloud pubsub topics describe $TOPIC_NAME --project=$PROJECT_ID &> /dev/null; then
    echo "Topic does not exist. Creating it..."
    gcloud pubsub topics create $TOPIC_NAME --project=$PROJECT_ID
    echo "Topic created successfully!"
else
    echo "Topic exists."
fi

# Grant Pub/Sub Publisher role
echo ""
echo "Granting Pub/Sub Publisher role to $EMAIL..."
gcloud pubsub topics add-iam-policy-binding $TOPIC_NAME \
    --member="user:$EMAIL" \
    --role="roles/pubsub.publisher" \
    --project=$PROJECT_ID

if [ $? -eq 0 ]; then
    echo "✓ Permission granted successfully!"
else
    echo "✗ Failed to grant permission. Please check:"
    echo "  1. You have permission to modify IAM policies"
    echo "  2. The email address is correct"
    echo "  3. You're in the correct project"
    exit 1
fi

# Also grant at project level (backup)
echo ""
echo "Also granting at project level (backup)..."
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="user:$EMAIL" \
    --role="roles/pubsub.publisher"

if [ $? -eq 0 ]; then
    echo "✓ Project-level permission granted!"
else
    echo "⚠ Could not grant project-level permission (this is optional)"
fi

# Verify permissions
echo ""
echo "Verifying permissions..."
echo "Current IAM policy for topic:"
gcloud pubsub topics get-iam-policy $TOPIC_NAME --project=$PROJECT_ID

echo ""
echo "=========================================="
echo "✓ Done! Wait 2-3 minutes for permissions to propagate."
echo "Then try the 'Automate' button again."
echo "=========================================="

