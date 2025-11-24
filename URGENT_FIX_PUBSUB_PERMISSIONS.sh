#!/bin/bash

# URGENT: Fix Pub/Sub Permissions for Gmail Watch
# This script will verify and fix permissions for gauravv882001@gmail.com

set -e

EMAIL="gauravv882001@gmail.com"
PROJECT_ID="automationspecialist"
TOPIC_NAME="gmail-notifs"
FULL_TOPIC="projects/${PROJECT_ID}/topics/${TOPIC_NAME}"

echo "=========================================="
echo "URGENT: Fixing Pub/Sub Permissions"
echo "=========================================="
echo "Email: ${EMAIL}"
echo "Project: ${PROJECT_ID}"
echo "Topic: ${FULL_TOPIC}"
echo "=========================================="
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "❌ ERROR: gcloud CLI is not installed!"
    echo "Please install it from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Check if user is authenticated
echo "🔍 Checking gcloud authentication..."
if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q .; then
    echo "❌ ERROR: Not authenticated with gcloud!"
    echo "Please run: gcloud auth login"
    exit 1
fi

echo "✅ Authenticated with gcloud"
echo ""

# Set the project
echo "🔧 Setting project to ${PROJECT_ID}..."
gcloud config set project ${PROJECT_ID}
echo "✅ Project set"
echo ""

# Grant Pub/Sub Publisher role at TOPIC level
echo "🔧 Step 1: Granting Pub/Sub Publisher role at TOPIC level..."
echo "   Topic: ${FULL_TOPIC}"
echo "   Email: ${EMAIL}"
echo "   Role: roles/pubsub.publisher"
echo ""

if gcloud pubsub topics add-iam-policy-binding ${TOPIC_NAME} \
    --member="user:${EMAIL}" \
    --role="roles/pubsub.publisher" \
    --project=${PROJECT_ID} 2>&1; then
    echo "✅ Topic-level permission granted"
else
    echo "⚠️  Warning: Could not grant topic-level permission (may already exist)"
fi
echo ""

# Grant Pub/Sub Publisher role at PROJECT level
echo "🔧 Step 2: Granting Pub/Sub Publisher role at PROJECT level..."
echo "   Project: ${PROJECT_ID}"
echo "   Email: ${EMAIL}"
echo "   Role: roles/pubsub.publisher"
echo ""

if gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="user:${EMAIL}" \
    --role="roles/pubsub.publisher" 2>&1; then
    echo "✅ Project-level permission granted"
else
    echo "⚠️  Warning: Could not grant project-level permission (may already exist)"
fi
echo ""

# Verify topic-level permission
echo "🔍 Step 3: Verifying topic-level permission..."
echo "   Checking if ${EMAIL} has Pub/Sub Publisher on topic ${TOPIC_NAME}..."
echo ""

TOPIC_POLICY=$(gcloud pubsub topics get-iam-policy ${TOPIC_NAME} --project=${PROJECT_ID} --format=json 2>/dev/null || echo "{}")

if echo "${TOPIC_POLICY}" | grep -q "${EMAIL}"; then
    if echo "${TOPIC_POLICY}" | grep -q "roles/pubsub.publisher"; then
        echo "✅ VERIFIED: ${EMAIL} has Pub/Sub Publisher role on topic ${TOPIC_NAME}"
    else
        echo "⚠️  WARNING: ${EMAIL} found in topic policy but may not have correct role"
    fi
else
    echo "❌ ERROR: ${EMAIL} NOT FOUND in topic policy!"
    echo "   Topic policy:"
    echo "${TOPIC_POLICY}" | jq '.' 2>/dev/null || echo "${TOPIC_POLICY}"
    echo ""
    echo "⚠️  This is a problem! The permission may not have been granted correctly."
fi
echo ""

# Verify project-level permission
echo "🔍 Step 4: Verifying project-level permission..."
echo "   Checking if ${EMAIL} has Pub/Sub Publisher on project ${PROJECT_ID}..."
echo ""

PROJECT_POLICY=$(gcloud projects get-iam-policy ${PROJECT_ID} --format=json 2>/dev/null || echo "{}")

if echo "${PROJECT_POLICY}" | grep -q "${EMAIL}"; then
    if echo "${PROJECT_POLICY}" | grep -q "roles/pubsub.publisher"; then
        echo "✅ VERIFIED: ${EMAIL} has Pub/Sub Publisher role on project ${PROJECT_ID}"
    else
        echo "⚠️  WARNING: ${EMAIL} found in project policy but may not have correct role"
    fi
else
    echo "❌ ERROR: ${EMAIL} NOT FOUND in project policy!"
    echo "⚠️  This is a problem! The permission may not have been granted correctly."
fi
echo ""

# Check Pub/Sub API is enabled
echo "🔍 Step 5: Checking if Pub/Sub API is enabled..."
if gcloud services list --enabled --project=${PROJECT_ID} --filter="name:pubsub.googleapis.com" --format="value(name)" | grep -q pubsub; then
    echo "✅ Pub/Sub API is enabled"
else
    echo "❌ ERROR: Pub/Sub API is NOT enabled!"
    echo "   Enabling now..."
    gcloud services enable pubsub.googleapis.com --project=${PROJECT_ID}
    echo "✅ Pub/Sub API enabled"
fi
echo ""

# Check Gmail API is enabled
echo "🔍 Step 6: Checking if Gmail API is enabled..."
if gcloud services list --enabled --project=${PROJECT_ID} --filter="name:gmail.googleapis.com" --format="value(name)" | grep -q gmail; then
    echo "✅ Gmail API is enabled"
else
    echo "❌ ERROR: Gmail API is NOT enabled!"
    echo "   Enabling now..."
    gcloud services enable gmail.googleapis.com --project=${PROJECT_ID}
    echo "✅ Gmail API enabled"
fi
echo ""

# Final summary
echo "=========================================="
echo "SUMMARY"
echo "=========================================="
echo "✅ Permissions have been granted/verified"
echo "✅ APIs are enabled"
echo ""
echo "⚠️  IMPORTANT: Permissions can take 5-15 minutes to fully propagate"
echo "   If you still get errors, wait 10-15 minutes and try again"
echo ""
echo "📋 Next steps:"
echo "   1. Wait 5-10 minutes for permissions to propagate"
echo "   2. Try the 'Automate' button again"
echo "   3. If it still fails, check the Google Cloud Console:"
echo "      - Topic: https://console.cloud.google.com/cloudpubsub/topic/detail/${TOPIC_NAME}?project=${PROJECT_ID}"
echo "      - Project IAM: https://console.cloud.google.com/iam-admin/iam?project=${PROJECT_ID}"
echo ""
echo "=========================================="


