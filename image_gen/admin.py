from django.contrib import admin
from image_gen.models import ImageGenerationJob, ReferenceImage
from image_gen.db_models.user import Users

# Register your models here.

@admin.register(ImageGenerationJob)
class ImageGenerationJobAdmin(admin.ModelAdmin):
    list_display = ['job_id', 'user', 'status', 'progress', 'created_at', 'image_url']
    list_filter = ['status', 'created_at', 'style', 'quality']
    search_fields = ['job_id', 'prompt', 'user__email']
    readonly_fields = ['job_id', 'image_id', 'created_at', 'started_at', 'completed_at']

@admin.register(ReferenceImage)
class ReferenceImageAdmin(admin.ModelAdmin):
    list_display = ['job', 'filename', 'created_at']
    list_filter = ['created_at']
    search_fields = ['job__job_id', 'filename']

@admin.register(Users)
class UsersAdmin(admin.ModelAdmin):
    list_display = ['uid', 'email', 'name', 'user_type', 'is_email_verified', 'mobile', 'created_at']
    list_filter = ['user_type', 'is_email_verified', 'created_at']
    search_fields = ['uid', 'email', 'name', 'mobile']
    readonly_fields = ['uid', 'created_at', 'updated_at']
