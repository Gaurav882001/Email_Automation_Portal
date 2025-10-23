from functools import wraps
from utils.response import ResponseView
from image_gen.db_models.user import Users
from utils.jwt_utils import verify_jwt_token
from utils.constant import FAIL, HTTP_UNAUTHORIZED, HTTP_TOKEN_EXPIRED
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

def user_token_auth(view_func):
    @wraps(view_func)
    def _wrapped_view(self, request, *args, **kwargs):
        auth_header = request.headers.get('Authorization')

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
            uid = decoded.get('uid')

            if not uid:
                return ResponseView.validation_error_response_data(
                message="Invalid token",
                code=HTTP_UNAUTHORIZED,
                )

            user = Users.objects.filter(uid=uid).first()

            if not user:
                return ResponseView.error_response_without_data(
                    message="Access denied.",
                    code=FAIL,
                    http_status=HTTP_UNAUTHORIZED
                )

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
            return ResponseView.internal_server_error_response()

    return _wrapped_view
