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