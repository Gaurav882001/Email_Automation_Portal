import random
from datetime import timedelta
from django.utils import timezone
from utils.mailer import send_otp_email
from utils.response import ResponseView
from rest_framework.views import APIView
from image_gen.db_models.user import Users
from utils.decorators import user_token_auth
from django.contrib.auth.hashers import make_password, check_password
from utils.jwt_utils import create_jwt_token, create_refresh_jwt_token, verify_refresh_jwt_token
from utils.constant import ( SUCCESS, EMAIL_SUCCESS, FAIL, HTTP_BAD_REQUEST, HTTP_UNAUTHORIZED, HTTP_NOT_FOUND )

class RegisterAdminView(APIView):
    def post(self, request):
        data = request.data

        required_fields = ['email', 'password']
        for field in required_fields:
            if not data.get(field):
                return ResponseView.success_response_without_data(
                    f"{field.capitalize()} is required.",
                    code=FAIL,
                )

        email = data.get('email')
        if Users.objects.filter(email=email).exists():
            return ResponseView.success_response_without_data(
                "Email already exists.",
                code=FAIL,
            )

        try:
            Users.objects.create(
                name=data.get('name'),
                email=email,
                mobile=data.get('mobile'),
                address=data.get('address'),
                password=make_password(data.get('password')),
                user_type='admin',
                is_email_verified=False
            )

            return ResponseView.success_response_without_data(
                message="Admin registered successfully.",
                code=SUCCESS
            )

        except Exception as e:
            print("RegisterAdmin error:", e)
            return ResponseView.internal_server_error_response()

class LoginView(APIView):
	def post(self, request):
		email = request.data.get('email')
		password = request.data.get('password')
		
		if not email or not password:
			return ResponseView.success_response_without_data(
				'Email and password required',
				code=FAIL,
			)
		
		try:
			user= Users.objects.get(email=email)
			
			if not check_password(password, user.password):
				return ResponseView.success_response_without_data(
					'Password is invalid',
					code=FAIL,
				)

			if not user.is_email_verified:
				otp = random.randint(1000, 9999)
				user.otp = otp
				user.save(update_fields=['otp'])

				# Send OTP via email
				email_sent = send_otp_email(user.email, user.name, otp)
				if not email_sent:
					print(f"⚠️ Failed to send OTP email to {user.email}")
					# Still return success message to user, but log the error
					# The OTP is saved in the database, so user can request it again if needed

				return ResponseView.success_response_without_data(
					message="OTP sent to your email. Please verify to continue.",
					code=EMAIL_SUCCESS,
				)

	
			user.last_login = timezone.now()
			user.save(update_fields=['last_login'])

			token = create_jwt_token(
				{'user_id': user.id, 'uid':user.uid, 'email': user.email},
				expires_delta=timedelta(days=1)
			)
			
			refreshToken = create_refresh_jwt_token(
				{'user_id': user.id, 'uid': user.uid, 'email': user.email},
				expires_delta=timedelta(days=30)
			)

			responseData = {
				'accessToken': token,
				'refreshToken': refreshToken,
				'userData': {
                    'userId': user.id,
                    'uid': user.uid,
					'email': user.email,
					'isEmailVerified': user.is_email_verified,
					'name' : user.name,
					'mobile' : user.mobile,
					'userType': user.user_type,
					'lastLogin' : user.last_login,
					'address': user.address,
				 },
			}
			
			return ResponseView.success_response_data(
	   			data=responseData,
				message="User login successfully",
				code=SUCCESS,
		  	)
			
		except Users.DoesNotExist:
			return ResponseView.success_response_without_data(
				"User not found",
				code=FAIL,
				http_status=HTTP_NOT_FOUND
			)
		
		except Exception as e:
			print(e)
			return ResponseView.internal_server_error_response()


class VerifyOtpView(APIView):
    def post(self, request):
        email = request.data.get('email')
        otp = request.data.get('otp')

        if not email or not otp:
            return ResponseView.success_response_without_data(
                'Email and OTP are required',
                code=FAIL,
            )

        try:
            user = Users.objects.get(email=email)

            if user.otp != int(otp):
                return ResponseView.success_response_without_data(
                    "Invalid OTP",
                    code=FAIL,
                    http_status=HTTP_UNAUTHORIZED
                )

            # Mark email as verified
            user.is_email_verified = True
            user.otp = None
            user.last_login = timezone.now()
            user.save(update_fields=['is_email_verified', 'otp', 'last_login'])

            token = create_jwt_token(
                {'user_id': user.id, 'uid': user.uid, 'email': user.email},
                expires_delta=timedelta(days=1)
            )

            refreshToken = create_refresh_jwt_token(
                {'user_id': user.id, 'uid': user.uid, 'email': user.email},
                expires_delta=timedelta(days=30)
            )

            responseData = {
                'accessToken': token,
                'refreshToken': refreshToken,
                'userData': {
                    'userId': user.id,
                    'uid': user.uid,
                    'email': user.email,
                    'isEmailVerified': user.is_email_verified,
                    'name': user.name,
                    'mobile': user.mobile,
                    'userType': user.user_type,
                    'lastLogin': user.last_login,
                    'address': user.address,
                },
            }

            return ResponseView.success_response_data(
                data=responseData,
                message="Email verified and login successful",
                code=SUCCESS
            )

        except Users.DoesNotExist:
            return ResponseView.success_response_without_data(
                "User not found",
                code=FAIL,
                http_status=HTTP_NOT_FOUND
            )

        except Exception:
            return ResponseView.internal_server_error_response()


class RefreshTokenView(APIView):
    def post(self, request):
        refresh_token = request.data.get('refreshToken')

        if not refresh_token:
            return ResponseView.success_response_without_data(
                'Refresh token is required',
                code=FAIL,
            )

        payload = verify_refresh_jwt_token(refresh_token)

        if not payload:
            return ResponseView.success_response_without_data(
                "Invalid or expired refresh token",
                code=FAIL,
                http_status=HTTP_UNAUTHORIZED
            )

        try:
            user_id = payload.get("user_id")
            uid = payload.get("uid")
            email = payload.get("email")

            user = Users.objects.get(id=user_id, email=email, uid=uid)

            # Note: status field removed as it doesn't exist in the model
            # Add status field to model if user status checking is needed

            access_token = create_jwt_token({
                "user_id": user.id,
                "uid": user.uid,
                "email": user.email,
            }, expires_delta=timedelta(days=1))

            new_refresh_token = create_refresh_jwt_token({
                "user_id": user.id,
                "uid": user.uid,
                "email": user.email,
            }, expires_delta=timedelta(days=30))

            responseData = {
                "accessToken": access_token,
                "refreshToken": new_refresh_token,
            }

            return ResponseView.success_response_data(
                data=responseData,
                message="Token refreshed successfully",
                code=SUCCESS
            )

        except Users.DoesNotExist:
            return ResponseView.success_response_without_data(
                "User not found",
                code=FAIL,
                http_status=HTTP_NOT_FOUND
            )

        except Exception as e:
            print(e)
            return ResponseView.internal_server_error_response()


class ForgotPasswordView(APIView):
    def post(self, request):
        email = request.data.get('email')

        if not email:
            return ResponseView.success_response_without_data(
                "Email is required",
                code=FAIL,
            )

        try:
            user = Users.objects.get(email=email)

            otp = random.randint(1000, 9999)
            user.otp = otp
            user.save(update_fields=['otp'])

            send_otp_email(user.email, user.name, otp)

            return ResponseView.success_response_without_data(
                message="OTP sent to your registered email.",
                code=EMAIL_SUCCESS
            )

        except Users.DoesNotExist:
            return ResponseView.success_response_without_data(
                "User not found",
                code=FAIL,
            )

        except Exception:
            return ResponseView.internal_server_error_response()


class ResetPasswordView(APIView):
    def post(self, request):
        email = request.data.get('email')
        otp = request.data.get('otp')
        new_password = request.data.get('newPassword')

        if not email or not otp or not new_password:
            return ResponseView.success_response_without_data(
                "Email, OTP and new password are required",
                code=FAIL,
            )

        try:
            user = Users.objects.get(email=email)

            if user.otp != int(otp):
                return ResponseView.success_response_without_data(
                    "Invalid OTP",
                    code=FAIL,
                    http_status=HTTP_UNAUTHORIZED
                )

            user.password = make_password(new_password)
            user.otp = None
            user.save(update_fields=['password', 'otp'])

            return ResponseView.success_response_without_data(
                message="Password reset successfully",
                code=SUCCESS
            )

        except Users.DoesNotExist:
            return ResponseView.success_response_without_data(
                "User not found",
                code=FAIL,
                http_status=HTTP_NOT_FOUND
            )

        except Exception:
            return ResponseView.internal_server_error_response()


class ChangePasswordView(APIView):
    # Add decorator for user validation
    @user_token_auth
    def post(self, request):
        current_password = request.data.get('currentPassword')
        new_password = request.data.get('newPassword')

        if not current_password or not new_password:
            return ResponseView.success_response_without_data(
                "Current and new passwords are required",
                code=FAIL,
            )

        user = request.auth_user  # from authentication token

        if not check_password(current_password, user.password):
            return ResponseView.success_response_without_data(
                "Current password is incorrect",
                code=FAIL,
            )

        user.password = make_password(new_password)
        user.save(update_fields=['password'])

        return ResponseView.success_response_without_data(
            message="Password changed successfully",
            code=SUCCESS
        )

