#!/bin/bash

# Comprehensive script to diagnose and fix Pub/Sub permissions for Gmail watch
# Usage: ./diagnose_and_fix_pubsub.sh gauravv882001@gmail.com

EMAIL="${1:-gauravv882001@gmail.com}"
PROJECT_ID="automationspecialist"
TOPIC_NAME="gmail-notifs"
FULL_TOPIC="projects/${PROJECT_ID}/topics/${TOPIC_NAME}"

echo "=========================================="
echo "🔍 Gmail Watch Pub/Sub Permission Fixer"
echo "=========================================="
echo "Email: $EMAIL"
echo "Project: $PROJECT_ID"
echo "Topic: $TOPIC_NAME"
echo "Full Topic: $FULL_TOPIC"
echo ""

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "❌ ERROR: gcloud CLI is not installed."
    echo "   Install from: https://cloud.google.com/sdk/docs/install"
    echo ""
    echo "   OR fix manually at:"
    echo "   https://console.cloud.google.com/cloudpubsub/topic/detail/$TOPIC_NAME?project=$PROJECT_ID"
    exit 1
fi

# Check if authenticated
CURRENT_USER=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -n1)
if [ -z "$CURRENT_USER" ]; then
    echo "⚠️  Not authenticated with gcloud."
    echo "   Run: gcloud auth login"
    exit 1
fi

echo "✓ Authenticated as: $CURRENT_USER"
echo ""

# Set the project
echo "Setting project to $PROJECT_ID..."
gcloud config set project $PROJECT_ID > /dev/null 2>&1

# Check if topic exists
echo "Checking if topic exists..."
if ! gcloud pubsub topics describe $TOPIC_NAME --project=$PROJECT_ID > /dev/null 2>&1; then
    echo "⚠️  Topic does not exist. Creating it..."
    if gcloud pubsub topics create $TOPIC_NAME --project=$PROJECT_ID; then
        echo "✓ Topic created successfully!"
    else
        echo "❌ Failed to create topic. Check your permissions."
        exit 1
    fi
else
    echo "✓ Topic exists."
fi

echo ""
echo "=========================================="
echo "📋 STEP 1: Checking Current Permissions"
echo "=========================================="

# Get current IAM policy
echo "Current IAM policy for topic '$TOPIC_NAME':"
POLICY=$(gcloud pubsub topics get-iam-policy $TOPIC_NAME --project=$PROJECT_ID 2>&1)

if [ $? -eq 0 ]; then
    echo "$POLICY"
    echo ""
    
    # Check if email has publisher role
    if echo "$POLICY" | grep -q "$EMAIL.*pubsub.publisher\|$EMAIL.*roles/pubsub.publisher"; then
        echo "✅ Email '$EMAIL' already has Pub/Sub Publisher permission!"
        echo ""
        echo "If you're still getting errors:"
        echo "  1. Wait 3-5 minutes for propagation"
        echo "  2. Verify the email matches exactly (case-sensitive)"
        echo "  3. Try the 'Automate' button again"
        exit 0
    else
        echo "❌ Email '$EMAIL' does NOT have Pub/Sub Publisher permission"
    fi
else
    echo "⚠️  Could not retrieve IAM policy (this is okay, we'll add permission anyway)"
fi

echo ""
echo "=========================================="
echo "🔧 STEP 2: Adding Pub/Sub Publisher Permission"
echo "=========================================="

# Grant permission at PROJECT level FIRST (Gmail API requires this!)
echo "=========================================="
echo "🔧 STEP 2a: Granting Permission at PROJECT Level"
echo "=========================================="
echo "⚠️  IMPORTANT: Gmail API requires permission at PROJECT level!"
echo "Granting Pub/Sub Publisher role to $EMAIL at PROJECT level..."
if gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="user:$EMAIL" \
    --role="roles/pubsub.publisher" 2>&1; then
    echo "✅ Project-level permission granted!"
else
    echo "❌ Failed to grant project-level permission"
    echo "   You may need to:"
    echo "   1. Have 'Project Owner' or 'Security Admin' role"
    echo "   2. Check that the email is correct"
    echo "   Continuing with topic-level permission anyway..."
fi

# Grant permission at topic level
echo ""
echo "=========================================="
echo "🔧 STEP 2b: Granting Permission at TOPIC Level"
echo "=========================================="
echo "Granting Pub/Sub Publisher role to $EMAIL at topic level..."
if gcloud pubsub topics add-iam-policy-binding $TOPIC_NAME \
    --member="user:$EMAIL" \
    --role="roles/pubsub.publisher" \
    --project=$PROJECT_ID 2>&1; then
    echo "✅ Topic-level permission granted!"
else
    echo "❌ Failed to grant topic-level permission"
    echo "   You may need to:"
    echo "   1. Have 'Pub/Sub Admin' or 'Project Owner' role"
    echo "   2. Check that the email is correct"
    if [ $? -ne 0 ] && ! gcloud projects get-iam-policy $PROJECT_ID --flatten="bindings[].members" --filter="bindings.members:user:$EMAIL" --format="value(bindings.role)" 2>/dev/null | grep -q "pubsub.publisher"; then
        echo "   ⚠️  Neither project nor topic permission was granted. Exiting."
        exit 1
    fi
fi

echo ""
echo "=========================================="
echo "✅ STEP 3: Verifying Permissions"
echo "=========================================="

# Wait a moment for propagation
echo "Waiting 2 seconds for permission propagation..."
sleep 2

# Verify topic-level permission
echo "Verifying topic-level permission..."
VERIFY_POLICY=$(gcloud pubsub topics get-iam-policy $TOPIC_NAME --project=$PROJECT_ID 2>&1)

if echo "$VERIFY_POLICY" | grep -q "$EMAIL.*pubsub.publisher\|$EMAIL.*roles/pubsub.publisher"; then
    echo "✅ VERIFIED: $EMAIL has Pub/Sub Publisher permission on topic!"
else
    echo "⚠️  WARNING: Permission not yet visible (may take 1-2 minutes to propagate)"
    echo "   Current policy:"
    echo "$VERIFY_POLICY" | grep -A 3 "$EMAIL" || echo "   (Email not found in policy yet)"
fi

echo ""
echo "=========================================="
echo "📝 SUMMARY"
echo "=========================================="
echo "✓ Permission granted to: $EMAIL"
echo "✓ Role: Pub/Sub Publisher (roles/pubsub.publisher)"
echo "✓ Topic: $FULL_TOPIC"
echo "✓ Project: $PROJECT_ID"
echo ""
echo "⏱️  IMPORTANT: Wait 3-5 minutes for permissions to fully propagate"
echo "   Then try the 'Automate' button again."
echo ""
echo "🔗 Verify manually at:"
echo "   https://console.cloud.google.com/cloudpubsub/topic/detail/$TOPIC_NAME?project=$PROJECT_ID"
echo "   → Click 'PERMISSIONS' tab"
echo "   → Look for: $EMAIL with role 'Pub/Sub Publisher'"
echo ""
echo "=========================================="

