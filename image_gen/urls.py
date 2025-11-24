from django.urls import path
from rest_framework.decorators import api_view

from .views import auth_view, general_view
from .views.image_generation_view import ImageGenerationView, ImageStatusView, JobListView, RetryJobView, DeleteJobView, DashboardStatsView, PromptGenerationView, RefinePromptView
from .views.video_generation_view import VideoGenerationView, VideoStatusView, VideoJobListView, VideoRetryJobView, VideoDeleteJobView, VideoDashboardStatsView, VideoPromptGenerationView, RefineVideoPromptView, VideoExtendView
from .views.avatar_generation_view import AvatarGenerationView, AvatarStatusView, AvatarJobListView, AvatarRetryJobView, AvatarDeleteJobView, AvatarImageView, AvatarImageFromHeyGenView, AvatarVoicesView, AvatarListFromHeyGenView, AssetListFromHeyGenView, AvatarPromptGenerationView, RefineAvatarPromptView, AvatarScriptGenerationView, AvatarScriptRefinementView
from .views.email_automation_view import EmailAutomationView, GmailPushWebhookView, EmailAccountListView, EmailAccountDeleteView, ProcessedEmailListView, ProcessedEmailDeleteView
from .views.oauth_view import GoogleOAuthCallbackView

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
    path('extend-video/', VideoExtendView.as_view(), name='extend-video'),
    
    # Video prompt generation
    path('generate-video-prompts/', VideoPromptGenerationView.as_view(), name='generate-video-prompts'),
    path('refine-video-prompt/', RefineVideoPromptView.as_view(), name='refine-video-prompt'),
    
    # Avatar generation with HeyGen
    path('generate-avatar/', AvatarGenerationView.as_view(), name='generate-avatar'),
    path('avatar-status/<str:job_id>/', AvatarStatusView.as_view(), name='avatar-status'),
    path('avatar-jobs/', AvatarJobListView.as_view(), name='avatar-job-list'),
    path('retry-avatar-job/<str:job_id>/', AvatarRetryJobView.as_view(), name='retry-avatar-job'),
    path('delete-avatar-job/<str:job_id>/', AvatarDeleteJobView.as_view(), name='delete-avatar-job'),
    path('avatar-images/<str:generation_id>/', AvatarImageView.as_view(), name='avatar-images'),
    path('avatar-image-heygen/<str:avatar_id>/', AvatarImageFromHeyGenView.as_view(), name='avatar-image-heygen'),
    path('avatar-voices/', AvatarVoicesView.as_view(), name='avatar-voices'),
    path('avatar-list-heygen/', AvatarListFromHeyGenView.as_view(), name='avatar-list-heygen'),
    path('asset-list-heygen/', AssetListFromHeyGenView.as_view(), name='asset-list-heygen'),
    
    # Avatar prompt generation
    path('generate-avatar-prompts/', AvatarPromptGenerationView.as_view(), name='generate-avatar-prompts'),
    path('refine-avatar-prompt/', RefineAvatarPromptView.as_view(), name='refine-avatar-prompt'),
    path('generate-avatar-script/', AvatarScriptGenerationView.as_view(), name='generate-avatar-script'),
    path('refine-avatar-script/', AvatarScriptRefinementView.as_view(), name='refine-avatar-script'),
    
    # Email automation
    path('email-automation/setup/', EmailAutomationView.as_view(), name='email-automation-setup'),
    path('email-accounts/', EmailAccountListView.as_view(), name='email-account-list'),
    path('email-accounts/<str:account_id>/', EmailAccountDeleteView.as_view(), name='email-account-delete'),
    path('processed-emails/', ProcessedEmailListView.as_view(), name='processed-email-list'),
    path('processed-emails/<str:email_id>/', ProcessedEmailDeleteView.as_view(), name='processed-email-delete'),
    path('pubsub/push/', GmailPushWebhookView.as_view(), name='gmail-push-webhook'),
    path('oauth/google/callback/', GoogleOAuthCallbackView.as_view(), name='google-oauth-callback'),

]