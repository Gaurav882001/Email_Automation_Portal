import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from_email = os.getenv('EMAIL_SENDER')
app_password = os.getenv('GMAIL_APP_PASSWORD')

def send_otp_email(to_email, name, otp):
    subject = 'OTP verification'
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 20px; font-family: Arial, sans-serif; background-color: #f4f7fa; line-height: 1.6;">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <!-- Header -->
            <tr>
                <td style="background-color: #667eea; padding: 40px 30px; text-align: center;">
                    <h1 style="margin: 0; color: white; font-size: 28px; font-weight: bold;">🔐 Verification Required</h1>
                </td>
            </tr>
            
            <!-- Content -->
            <tr>
                <td style="padding: 40px 30px; text-align: center;">
                    <h2 style="color: #333; font-size: 20px; margin-bottom: 20px;">Hello there! 👋</h2>
                    <p style="color: #666; font-size: 16px; margin-bottom: 30px;">
                        We received a request to verify your account. Please use the verification code below to complete the process.
                    </p>
                    
                    <!-- OTP Container -->
                    <table cellpadding="0" cellspacing="0" style="margin: 30px auto; background-color: #ff6b6b; border-radius: 8px; overflow: hidden;">
                        <tr>
                            <td style="padding: 30px; text-align: center;">
                                <div style="color: white; font-size: 14px; margin-bottom: 10px;">Your Verification Code</div>
                                <div style="color: white; font-size: 36px; font-weight: bold; letter-spacing: 6px; margin: 0;">{otp}</div>
                            </td>
                        </tr>
                    </table>
                    
                    <!-- Expiry Notice -->
                    <table cellpadding="0" cellspacing="0" style="margin: 20px auto; background-color: #fff3cd; border: 1px solid #ffeaa7; border-radius: 6px;">
                        <tr>
                            <td style="padding: 15px; text-align: center; color: #856404; font-size: 14px;">
                                ⏰ This code will expire in <strong>10 minutes</strong> for your security.
                            </td>
                        </tr>
                    </table>
                    
                    <!-- Security Note -->
                    <table cellpadding="0" cellspacing="0" style="margin: 20px auto; background-color: #e3f2fd; border-left: 4px solid #2196f3; border-radius: 4px;">
                        <tr>
                            <td style="padding: 15px; color: #1565c0; font-size: 14px;">
                                🛡️ <strong>Security Tip:</strong> Never share this code with anyone. Our team will never ask for your verification code.
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
            
            <!-- Footer -->
            <tr>
                <td style="background-color: #f8f9fa; padding: 30px; text-align: center; border-top: 1px solid #e9ecef;">
                    <p style="margin: 0; color: #666; font-size: 14px;">
                        Best regards,<br>
                        <span style="color: #667eea; font-weight: bold;">The Image gen Team</span>
                    </p>
                    <p style="margin-top: 15px; font-size: 12px; color: #999;">
                        If you didn't request this code, please ignore this email.
                    </p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    # Use EMAIL_SENDER from environment variable as the sender email for OTP emails
    otp_from_email = os.getenv('EMAIL_SENDER')
    # Use the app password from environment variable - try OTP-specific one first, then fallback to general one
    otp_app_password = os.getenv('OTP_SENDER_APP_PASSWORD') or os.getenv('GMAIL_APP_PASSWORD')

    # Validate email sender is set
    if not otp_from_email:
        print(f"❌ ERROR: EMAIL_SENDER environment variable is not set!")
        print(f"   Cannot send OTP email to {to_email}")
        print(f"   Please set EMAIL_SENDER in your .env file")
        return False

    # Validate app password is set
    if not otp_app_password:
        print(f"❌ ERROR: Neither OTP_SENDER_APP_PASSWORD nor GMAIL_APP_PASSWORD environment variable is set!")
        print(f"   Cannot send OTP email from {otp_from_email} to {to_email}")
        print(f"   Please set OTP_SENDER_APP_PASSWORD or GMAIL_APP_PASSWORD with the app password for {otp_from_email}")
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = otp_from_email
    msg['To'] = to_email

    msg.attach(MIMEText(html_content, 'html'))

    try:
        print(f"📧 Attempting to send OTP email from {otp_from_email} to {to_email}")
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            print(f"🔐 Attempting SMTP login with {otp_from_email}")
            server.login(otp_from_email, otp_app_password)
            print(f"✅ SMTP login successful")
            server.sendmail(otp_from_email, to_email, msg.as_string())
        print(f"✅ OTP email sent successfully from {otp_from_email} to {to_email}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ SMTP Authentication Error: {e}")
        print(f"   The app password in GMAIL_APP_PASSWORD is not valid for {otp_from_email}")
        print(f"   SOLUTION: Generate a Gmail App Password for {otp_from_email}:")
        print(f"   1. Go to https://myaccount.google.com/apppasswords")
        print(f"   2. Sign in with {otp_from_email}")
        print(f"   3. Generate a new app password")
        print(f"   4. Set it in your .env file as: GMAIL_APP_PASSWORD=<generated-app-password>")
        print(f"   OR set it as: OTP_SENDER_APP_PASSWORD=<generated-app-password>")
        print(f"   Error details: {str(e)}")
        return False
    except Exception as e:
        print(f"❌ Error sending OTP email from {otp_from_email} to {to_email}: {e}")
        import traceback
        print(f"   Traceback: {traceback.format_exc()}")
        return False


def send_invite_email(to_email, inviter_name, invite_url):
    subject = "You're Invited to Join the ImageGen!"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 20px; font-family: Arial, sans-serif; background-color: #f4f7fa; line-height: 1.6;">
        <table width="100%" cellpadding="0" cellspacing="0" style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
            <!-- Header -->
            <tr>
                <td style="background-color: #4facfe; padding: 40px 30px; text-align: center;">
                    <h1 style="margin: 0; color: white; font-size: 28px; font-weight: bold;">🎉 You're Invited!</h1>
                </td>
            </tr>
            
            <!-- Content -->
            <tr>
                <td style="padding: 40px 30px;">
                    <h2 style="color: #333; font-size: 20px; margin-bottom: 20px; text-align: center;">Hello there! 👋</h2>
                    
                    <!-- Invitation Card -->
                    <table cellpadding="0" cellspacing="0" style="width: 100%; margin: 25px 0; background-color: #667eea; border-radius: 8px;">
                        <tr>
                            <td style="padding: 25px; text-align: center; color: white;">
                                <div style="font-size: 16px;">You have been invited to join</div>
                            </td>
                        </tr>
                    </table>
                                        
                    <!-- CTA Button -->
                    <table cellpadding="0" cellspacing="0" style="margin: 25px auto;">
                        <tr>
                            <td style="text-align: center;">
                                <a href="{invite_url}" style="display: inline-block; background-color: #ff6b6b; color: white; text-decoration: none; padding: 16px 32px; border-radius: 25px; font-weight: bold; font-size: 16px;">
                                    🚀 Accept Invitation & Set Password
                                </a>
                            </td>
                        </tr>
                    </table>
                    
                    <!-- Features -->
                    <table cellpadding="0" cellspacing="0" style="width: 100%; margin: 25px 0; background-color: #f8f9fa; border-radius: 8px;">
                        <tr>
                            <td style="padding: 20px;">
                                <h3 style="color: #333; margin-top: 0; font-size: 18px;">What you'll get access to:</h3>
                                <table cellpadding="0" cellspacing="0" style="width: 100%;">
                                    <tr><td style="padding: 4px 0; color: #666; font-size: 14px;">✅ Shared content library and templates</td></tr>
                                    <tr><td style="padding: 4px 0; color: #666; font-size: 14px;">✅ Team workspace with role-based permissions</td></tr>
                                    <tr><td style="padding: 4px 0; color: #666; font-size: 14px;">✅ Analytics and performance tracking</td></tr>
                                </table>
                            </td>
                        </tr>
                    </table>
                    
                    <!-- Expiry Notice -->
                    <table cellpadding="0" cellspacing="0" style="width: 100%; margin: 20px 0; background-color: #fff3cd; border: 1px solid #ffeaa7; border-radius: 6px;">
                        <tr>
                            <td style="padding: 15px; text-align: center; color: #856404; font-size: 14px;">
                                ⏰ This invitation will expire in <strong>3 days</strong>. Don't miss out!
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
            
            <!-- Footer -->
            <tr>
                <td style="background-color: #f8f9fa; padding: 30px; text-align: center; border-top: 1px solid #e9ecef;">
                    <p style="margin: 0; color: #666; font-size: 14px;">
                        Welcome to the team!<br>
                        <span style="color: #4facfe; font-weight: bold;">The Image Gen Team</span>
                    </p>
                    <div style="font-size: 12px; color: #999; margin-top: 20px;">
                        Having trouble with the button? Copy and paste this link into your browser:<br>
                        <span style="word-break: break-all; color: #4facfe;">{invite_url}</span>
                    </div>
                    <p style="margin-top: 15px; font-size: 12px; color: #999;">
                        If you weren't expecting this invitation, you can safely ignore this email.
                    </p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = to_email
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(from_email, app_password)
            server.sendmail(from_email, to_email, msg.as_string())
        print("Invite email sent to", to_email)
        return True
    except Exception as e:
        print("Error sending invite email:", e)
        return False
