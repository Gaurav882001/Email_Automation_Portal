# Gmail Watch Code Updates - Aligned with Google Documentation

## Changes Made

Based on the official Google Cloud Console documentation for Gmail watch, the following updates have been made to align our code with best practices:

### 1. ✅ Added `labelFilterBehavior` to Watch Request

**Before:**
```python
request_body = {
    'labelIds': ['INBOX'],
    'topicName': topic_name
}
```

**After (matching Google docs):**
```python
request_body = {
    'labelIds': ['INBOX'],
    'topicName': topic_name,
    'labelFilterBehavior': 'INCLUDE'  # Include only messages with these labels
}
```

### 2. ✅ Proper Watch Response Handling

**Added:**
- Logging of `historyId` from response
- Logging of `expiration` timestamp from response
- Conversion of expiration timestamp (milliseconds) to datetime
- Warnings about watch renewal requirements

**Google Documentation:**
> Watch response contains:
> - `historyId`: Current mailbox historyId for the user
> - `expiration`: Timestamp for watch expiration

### 3. ✅ Fixed Base64URL Decoding

**Before:**
```python
message_data = base64.b64decode(envelope['message']['data']).decode('utf-8')
```

**After (matching Google docs):**
```python
# Google uses base64url encoding (URL-safe base64)
message_data_encoded = envelope['message']['data']
# Add padding if needed
padding = len(message_data_encoded) % 4
if padding:
    message_data_encoded += '=' * (4 - padding)
message_data = base64.urlsafe_b64decode(message_data_encoded).decode('utf-8')
```

**Google Documentation:**
> The message.data field is a base64url-encoded string

### 4. ✅ Watch Expiration Storage

**Updated:**
- Now stores actual expiration timestamp from Gmail API response
- Falls back to 7 days if expiration not in response
- Logs expiration warnings

**Google Documentation:**
> You must re-call watch at least every 7 days or else you will stop receiving updates for the user. We recommend calling watch once per day.

### 5. ✅ Watch Renewal Reminder

**Added:**
- Logging of watch expiration time
- Warnings about renewal requirements
- Notes that watch must be renewed at least every 7 days

## What Still Needs to Be Done

### ⚠️ Watch Renewal Mechanism (Not Yet Implemented)

According to Google documentation:
- Watch must be renewed **at least every 7 days**
- **Recommended: once per day**

**To Implement:**
1. Create a scheduled task (cron job or Celery task)
2. Check `watch_expiration` for all active `EmailAccount` records
3. Renew watches that expire within 24 hours
4. Run daily (or more frequently)

**Example Implementation:**
```python
# In a management command or scheduled task
def renew_gmail_watches():
    """Renew Gmail watches that expire soon"""
    from datetime import timedelta
    from django.utils import timezone
    
    # Find watches expiring in next 24 hours
    soon_to_expire = EmailAccount.objects.filter(
        is_active=True,
        is_automated=True,
        watch_expiration__lte=timezone.now() + timedelta(days=1)
    )
    
    for account in soon_to_expire:
        try:
            # Re-call watch API with same parameters
            # Store new historyId and expiration
            pass
        except Exception as e:
            print(f"Error renewing watch for {account.email}: {e}")
```

## Summary

✅ **Code now matches Google documentation:**
- Watch request includes `labelFilterBehavior`
- Watch response properly parsed (historyId, expiration)
- Base64URL decoding fixed
- Expiration stored and logged

⚠️ **Still needed:**
- Watch renewal mechanism (scheduled task)
- Automatic renewal before expiration

## Testing

After these changes:
1. Test watch setup - should work the same
2. Verify expiration is stored correctly
3. Check logs for expiration warnings
4. Plan watch renewal implementation

