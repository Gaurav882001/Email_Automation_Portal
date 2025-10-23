from django.urls import path
from rest_framework.decorators import api_view

from .views import auth_view, general_view
from .views.image_generation_view import ImageGenerationView, ImageStatusView, JobListView, RetryJobView, DeleteJobView, DashboardStatsView

urlpatterns = [
    
    # Server status check
    path('', general_view.HealthCheckView.as_view(), name='server-status-check'),
    
    # Auth view
    path('login/', auth_view.LoginView.as_view(), name='login'),
    path('verify-otp/', auth_view.VerifyOtpView.as_view(), name='verify-otp'),
    path('forget-password/', auth_view.ForgotPasswordView.as_view(), name='forget-password'),
    path('reset-password/', auth_view.ResetPasswordView.as_view(), name='reset-password'),
    path('change-password/', auth_view.ChangePasswordView.as_view(), name='change-password'),
    path('refresh-token/', auth_view.RefreshTokenView.as_view(), name='refresh-token'),
    path('register/', auth_view.RegisterAdminView.as_view(), name='register'),

    # Async Image generation with Nano Banana
    path('generate-image/', ImageGenerationView.as_view(), name='generate-image'),
    path('image-status/<str:job_id>/', ImageStatusView.as_view(), name='image-status'),
    path('jobs/', JobListView.as_view(), name='job-list'),
    path('retry-job/<str:job_id>/', RetryJobView.as_view(), name='retry-job'),
    path('delete-job/<str:job_id>/', DeleteJobView.as_view(), name='delete-job'),
    path('dashboard-stats/', DashboardStatsView.as_view(), name='dashboard-stats'),

]