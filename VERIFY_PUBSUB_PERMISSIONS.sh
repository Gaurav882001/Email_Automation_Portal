#!/bin/bash

# Quick verification script for Pub/Sub permissions

EMAIL="gauravv882001@gmail.com"
PROJECT_ID="automationspecialist"
TOPIC_NAME="gmail-notifs"

echo "=========================================="
echo "Verifying Pub/Sub Permissions"
echo "=========================================="
echo "Email: ${EMAIL}"
echo "Project: ${PROJECT_ID}"
echo "Topic: ${TOPIC_NAME}"
echo "=========================================="
echo ""

# Check topic-level permission
echo "📋 Checking TOPIC-level permission..."
TOPIC_POLICY=$(gcloud pubsub topics get-iam-policy ${TOPIC_NAME} --project=${PROJECT_ID} 2>&1)

if echo "${TOPIC_POLICY}" | grep -q "${EMAIL}"; then
    if echo "${TOPIC_POLICY}" | grep -q "pubsub.publisher"; then
        echo "✅ Topic-level: ${EMAIL} HAS Pub/Sub Publisher role"
    else
        echo "❌ Topic-level: ${EMAIL} found but WRONG role"
    fi
else
    echo "❌ Topic-level: ${EMAIL} NOT FOUND"
fi
echo ""

# Check project-level permission
echo "📋 Checking PROJECT-level permission..."
PROJECT_POLICY=$(gcloud projects get-iam-policy ${PROJECT_ID} --flatten="bindings[].members" --filter="bindings.members:user:${EMAIL}" --format="value(bindings.role)" 2>&1)

if echo "${PROJECT_POLICY}" | grep -q "pubsub.publisher"; then
    echo "✅ Project-level: ${EMAIL} HAS Pub/Sub Publisher role"
else
    echo "❌ Project-level: ${EMAIL} does NOT have Pub/Sub Publisher role"
fi
echo ""

echo "=========================================="
echo "Manual Verification Links:"
echo "=========================================="
echo "Topic Permissions:"
echo "https://console.cloud.google.com/cloudpubsub/topic/detail/${TOPIC_NAME}?project=${PROJECT_ID}"
echo ""
echo "Project IAM:"
echo "https://console.cloud.google.com/iam-admin/iam?project=${PROJECT_ID}"
echo "=========================================="


