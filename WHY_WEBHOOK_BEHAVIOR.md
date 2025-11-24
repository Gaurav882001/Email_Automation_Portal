# Understanding Webhook Behavior

## Why Webhook is Called for New Accounts But Not Existing Ones

### When Account is NEW (First Time Setup)

1. You click "Automate" → Sets up Gmail watch
2. Gmail sends a **test notification** to verify Pub/Sub works ✅
3. Webhook is called immediately ✅
4. This is Gmail's way of testing the setup

### When Account ALREADY EXISTS

1. You click "Automate" → Updates Gmail watch
2. Gmail does **NOT** send a test notification ❌
3. Webhook is **NOT** called ❌
4. Gmail assumes the setup is already verified

## This is Expected Behavior!

**Webhooks are NOT triggered by button clicks** - they're triggered by:
- ✅ Gmail sending notifications (when new emails arrive)
- ✅ Test notifications (only on first setup)
- ❌ NOT by clicking buttons
- ❌ NOT by updating watch settings

## The Solution: Immediate Processing

I've updated the code to:
1. **Process emails immediately** when you click "Automate"
2. **Use the OLD history ID** to get emails since last processing
3. **Show email subjects** in terminal
4. **Save invoice emails** to Drive right away

## How It Works Now

**When you click "Automate" (account exists):**
```
1. Get OLD history ID from database
2. Set up/update Gmail watch → Get NEW history ID
3. Process emails using OLD history ID (gets emails since last check)
4. Update database with NEW history ID
5. Show email subjects in terminal
```

**Future emails (automatic):**
```
1. New email arrives in Gmail
2. Gmail sends notification to Pub/Sub
3. Pub/Sub calls webhook
4. Webhook processes email
```

## Why "Found 0 message(s)" Appears

If you see "Found 0 message(s) to process", it means:
- ✅ No new emails arrived since the last processing
- ✅ All existing emails were already processed
- ✅ This is normal if no new invoices arrived

## Testing

To test if it's working:
1. Send a test invoice email to the account
2. Wait 1-2 minutes
3. Click "Automate" button
4. You should see the email subject in terminal

Or wait for the webhook (automatic):
1. Send a test invoice email
2. Wait 1-2 minutes
3. Webhook should be called automatically
4. Check terminal for webhook logs

## Summary

- **Webhook for new accounts**: Called once (test notification)
- **Webhook for existing accounts**: Only called when NEW emails arrive
- **Immediate processing**: Now happens on "Automate" click (uses old history ID)
- **Future processing**: Webhook handles new emails automatically

The webhook behavior is correct - it's just that Gmail only sends test notifications on first setup!

