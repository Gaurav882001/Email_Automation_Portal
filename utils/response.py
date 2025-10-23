from rest_framework import status
from rest_framework.response import Response

class ResponseView:
    @staticmethod
    def success_response_data(data, message="Success", code=1, extras=None, http_status=status.HTTP_200_OK):
        response = {
            "data": data,
            "meta": {
                "code": code,
                "message": message
            }
        }
        if extras:
            response["meta"].update(extras)
        return Response(response, status=http_status)

    @staticmethod
    def success_response_without_data(message="Success", code=1, extras=None, http_status=status.HTTP_200_OK):
        response = {
            "data": None,
            "meta": {
                "code": code,
                "message": message
            }
        }
        if extras:
            response["meta"].update(extras)
        return Response(response, status=http_status)

    @staticmethod
    def error_response_data(message="Bad Request", code=400, http_status=status.HTTP_400_BAD_REQUEST):
        response = {
            "code": code,
            "message": message
        }
        return Response(response, status=http_status)

    @staticmethod
    def error_response_without_data(message="Error", code=0, http_status=status.HTTP_400_BAD_REQUEST):
        response = {
            "data": None,
            "meta": {
                "code": code,
                "message": message
            }
        }
        return Response(response, status=http_status)

    @staticmethod
    def validation_error_response_data(message="Validation Error", code=400, http_status=status.HTTP_400_BAD_REQUEST):
        response = {
            "code": code,
            "message": message
        }
        return Response(response, status=http_status)

    @staticmethod
    def internal_server_error_response(message="Internal Server Error"):
        response = {
            "code": 500,
            "message": message
        }
        return Response(response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ResponseInfo:
    """
    Response formatter that matches React frontend expectations
    """
    @staticmethod
    def success(data, message="Success"):
        return {
            "meta": {
                "code": 1,
                "message": message
            },
            "data": data
        }
    
    @staticmethod
    def error(message="Error"):
        return {
            "meta": {
                "code": 0,
                "message": message
            },
            "data": None
        }
