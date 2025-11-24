from image_gen.db_models.user import Users
from django.db import models
import uuid

class ImageGenerationJob(models.Model):
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='image_jobs', null=True, blank=True)
    prompt = models.TextField()
    style = models.CharField(max_length=50)
    quality = models.CharField(max_length=50)
    status = models.CharField(max_length=20, default='queued')
    progress = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    image_url = models.URLField(null=True, blank=True)
    image_id = models.UUIDField(null=True, blank=True)
    provider = models.CharField(max_length=100, null=True, blank=True)
    dimensions = models.CharField(max_length=20, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Job {self.job_id} - {self.status} - User: {self.user.email if self.user else 'No User'}"


class ReferenceImage(models.Model):
    job = models.ForeignKey(ImageGenerationJob, on_delete=models.CASCADE, related_name='reference_images')
    image_data = models.TextField()  # Base64 encoded image data
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reference image for {self.job.job_id}"


class VideoGenerationJob(models.Model):
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='video_jobs', null=True, blank=True)
    prompt = models.TextField()  # Enhanced prompt (final prompt used for generation)
    original_prompt = models.TextField(null=True, blank=True)  # User's original prompt
    style = models.CharField(max_length=50, default='realistic')
    quality = models.CharField(max_length=50, default='high')
    duration = models.IntegerField(default=5)  # Duration in seconds
    status = models.CharField(max_length=20, default='queued')
    progress = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    video_url = models.URLField(null=True, blank=True)
    video_file_path = models.CharField(max_length=500, null=True, blank=True)
    provider = models.CharField(max_length=100, default='veo-3.1')
    error_message = models.TextField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Video Job {self.job_id} - {self.status} - User: {self.user.email if self.user else 'No User'}"


class VideoReferenceImage(models.Model):
    job = models.ForeignKey(VideoGenerationJob, on_delete=models.CASCADE, related_name='reference_images')
    image_data = models.TextField()  # Base64 encoded image data
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    reference_type = models.CharField(max_length=50, default='asset')  # 'asset' or other types
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reference image for {self.job.job_id}"


class AvatarGenerationJob(models.Model):
    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='avatar_jobs', null=True, blank=True)
    prompt = models.TextField()  # User's prompt/description for avatar (appearance field)
    name = models.CharField(max_length=255, null=True, blank=True)  # Avatar name
    age = models.CharField(max_length=50, null=True, blank=True)  # Young Adult, Early Middle Age, Late Middle Age, Senior, Unspecified
    gender = models.CharField(max_length=50, null=True, blank=True)  # Woman, Man, Unspecified
    ethnicity = models.CharField(max_length=100, null=True, blank=True)  # White, Black, Asian American, etc.
    orientation = models.CharField(max_length=50, null=True, blank=True)  # square, horizontal, vertical
    pose = models.CharField(max_length=50, null=True, blank=True)  # half_body, close_up, full_body
    style = models.CharField(max_length=50, null=True, blank=True)  # Realistic, Pixar, Cinematic, Vintage, Noir, Cyberpunk
    generation_id = models.CharField(max_length=255, null=True, blank=True)  # HeyGen generation_id for status checking
    status = models.CharField(max_length=20, default='queued')
    progress = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    avatar_url = models.URLField(max_length=2000, null=True, blank=True)  # URL to the generated avatar
    avatar_id = models.CharField(max_length=255, null=True, blank=True)  # HeyGen avatar ID
    image_key = models.CharField(max_length=500, null=True, blank=True)  # HeyGen image key from upload
    thumbnail_url = models.URLField(max_length=2000, null=True, blank=True)  # Thumbnail URL for video avatars
    provider = models.CharField(max_length=100, default='heygen')
    error_message = models.TextField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Avatar Job {self.job_id} - {self.status} - User: {self.user.email if self.user else 'No User'}"


class AvatarReferenceImage(models.Model):
    job = models.ForeignKey(AvatarGenerationJob, on_delete=models.CASCADE, related_name='reference_images')
    image_data = models.TextField()  # Base64 encoded image data
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reference image for avatar job {self.job.job_id}"


class EmailAccount(models.Model):
    account_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name='email_accounts')
    email = models.EmailField()
    credentials = models.JSONField()  # Store OAuth credentials
    watch_history_id = models.CharField(max_length=255, null=True, blank=True)
    watch_expiration = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_automated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['user', 'email']

    def __str__(self):
        return f"{self.email} - {self.user.email}"


class ProcessedEmail(models.Model):
    email_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email_account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='processed_emails')
    gmail_message_id = models.CharField(max_length=255, unique=True)
    subject = models.CharField(max_length=500)
    sender = models.EmailField()
    received_date = models.DateTimeField()
    drive_file_id = models.CharField(max_length=255, null=True, blank=True)
    drive_folder_name = models.CharField(max_length=50)  # Month name like "November"
    attachments = models.JSONField(default=list, blank=True)  # Store attachment info: [{"filename": "...", "file_id": "...", "mime_type": "..."}]
    is_invoice = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_date']

    def __str__(self):
        return f"Email {self.gmail_message_id} - {self.subject[:50]}"