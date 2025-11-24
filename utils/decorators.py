from functools import wraps
from utils.response import ResponseView
from image_gen.db_models.user import Users
from utils.jwt_utils import verify_jwt_token
from utils.constant import FAIL, HTTP_UNAUTHORIZED, HTTP_TOKEN_EXPIRED
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

def user_token_auth(view_func):
    @wraps(view_func)
    def _wrapped_view(self, request, *args, **kwargs):
        # Try both headers.get() and META.get() for Authorization header
        auth_header = None
        if hasattr(request, 'headers'):
            auth_header = request.headers.get('Authorization')
        if not auth_header and hasattr(request, 'META'):
            auth_header = request.META.get('HTTP_AUTHORIZATION')

        if not auth_header or not auth_header.startswith('Bearer '):
            return ResponseView.error_response_without_data(
                message="Unauthorized access",
                code=FAIL,
                http_status=HTTP_UNAUTHORIZED
            )

        token = auth_header.split(' ')[1]

        try:
            decoded = verify_jwt_token(token)
            if not decoded:
                return ResponseView.success_response_without_data(
                message="Token has expired",
                code=HTTP_TOKEN_EXPIRED,
                http_status=HTTP_TOKEN_EXPIRED
            )
            
            # Try both 'uid' and 'user_id' as JWT payload might use either
            uid = decoded.get('uid') or decoded.get('user_id')
            
            if not uid:
                print(f"JWT payload keys: {list(decoded.keys())}")
                print(f"JWT payload: {decoded}")
                return ResponseView.validation_error_response_data(
                message="Invalid token - missing user identifier",
                code=HTTP_UNAUTHORIZED,
                )

            # Try filtering by uid first, then by id
            user = Users.objects.filter(uid=uid).first()
            if not user:
                user = Users.objects.filter(id=uid).first()

            if not user:
                return ResponseView.error_response_without_data(
                    message="Access denied.",
                    code=FAIL,
                    http_status=HTTP_UNAUTHORIZED
                )

            # Check user status if the attribute exists
            # Use hasattr to avoid AttributeError if status field doesn't exist
            if hasattr(user, 'status'):
                if user.status == 'inactive':
                    return ResponseView.error_response_without_data(
                        message="Your account is inactive",
                        code=FAIL,
                        http_status=HTTP_UNAUTHORIZED
                    )

                if user.status == 'deleted':
                    return ResponseView.error_response_without_data(
                        message="Your account is deleted",
                        code=FAIL,
                        http_status=HTTP_UNAUTHORIZED
                    )
            
            # Alternative: check is_active if status doesn't exist
            if hasattr(user, 'is_active') and not user.is_active:
                return ResponseView.error_response_without_data(
                    message="Your account is inactive",
                    code=FAIL,
                    http_status=HTTP_UNAUTHORIZED
                )

            request.auth_user = user
            return view_func(self, request, *args, **kwargs)

        except ExpiredSignatureError:
            return ResponseView.error_response_without_data(
                message="Token has expired",
                code=FAIL,
                http_status=HTTP_UNAUTHORIZED
            )
        except InvalidTokenError:
            return ResponseView.error_response_without_data(
                message="Invalid token",
                code=FAIL,
                http_status=HTTP_UNAUTHORIZED
            )
        except Exception as e:
            import traceback
            print("=" * 80)
            print("USER_TOKEN_AUTH DECORATOR ERROR")
            print("=" * 80)
            print(f"Error Type: {type(e).__name__}")
            print(f"Error Message: {str(e)}")
            print(f"Full Traceback:")
            print(traceback.format_exc())
            print("=" * 80)
            return ResponseView.internal_server_error_response()

    return _wrapped_view
