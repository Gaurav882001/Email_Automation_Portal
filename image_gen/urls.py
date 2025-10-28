from django.urls import path
from rest_framework.decorators import api_view

from .views import auth_view, general_view
from .views.image_generation_view import ImageGenerationView, ImageStatusView, JobListView, RetryJobView, DeleteJobView, DashboardStatsView, PromptGenerationView, RefinePromptView
from .views.video_generation_view import VideoGenerationView, VideoStatusView, VideoJobListView, VideoRetryJobView, VideoDeleteJobView, VideoDashboardStatsView

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

    # Prompt generation
    path('generate-prompts/', PromptGenerationView.as_view(), name='generate-prompts'),
    path('refine-prompt/', RefinePromptView.as_view(), name='refine-prompt'),
    
    # Async Image generation with Nano Banana
    path('generate-image/', ImageGenerationView.as_view(), name='generate-image'),
    path('image-status/<str:job_id>/', ImageStatusView.as_view(), name='image-status'),
    path('jobs/', JobListView.as_view(), name='job-list'),
    path('retry-job/<str:job_id>/', RetryJobView.as_view(), name='retry-job'),
    path('delete-job/<str:job_id>/', DeleteJobView.as_view(), name='delete-job'),
    path('dashboard-stats/', DashboardStatsView.as_view(), name='dashboard-stats'),

    # Video generation with Google Veo 3.1
    path('generate-video/', VideoGenerationView.as_view(), name='generate-video'),
    path('video-status/<str:job_id>/', VideoStatusView.as_view(), name='video-status'),
    path('video-jobs/', VideoJobListView.as_view(), name='video-job-list'),
    path('retry-video-job/<str:job_id>/', VideoRetryJobView.as_view(), name='retry-video-job'),
    path('delete-video-job/<str:job_id>/', VideoDeleteJobView.as_view(), name='delete-video-job'),
    path('video-dashboard-stats/', VideoDashboardStatsView.as_view(), name='video-dashboard-stats'),

]