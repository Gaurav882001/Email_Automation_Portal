from utils.response import ResponseView
from rest_framework.views import APIView
from utils.constant import ( SUCCESS )

class HealthCheckView(APIView):
    def get(self, request):
        return ResponseView.success_response_without_data(
            message="Server is up and running.",
            code=SUCCESS
        )
