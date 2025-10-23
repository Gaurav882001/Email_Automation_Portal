from django.db import models
import uuid
from utils.constant import USER_TYPE
from django.core.validators import MinValueValidator, MaxValueValidator


class Users(models.Model):
	uid = models.CharField(max_length=36, default=uuid.uuid4, unique=True)
	email = models.EmailField(max_length=50, unique=True)
	user_type = models.CharField(choices=USER_TYPE, default='user')
	password = models.CharField(max_length=255)
	last_login = models.DateTimeField(null=True, blank=True)
	otp = models.IntegerField(
		validators=[MinValueValidator(1000), MaxValueValidator(9999)],
		null=True,
		blank=True
	)
	mobile = models.CharField(max_length=100, null=True, blank=True)
	name = models.CharField(max_length=100, null=True, blank=True)
	is_email_verified = models.BooleanField(default=False)
	address = models.CharField(max_length=255, null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	
	class Meta:
		constraints = [
			models.UniqueConstraint(
				fields=['email', 'user_type'],
				name='unique_email_user_type'
			)
		]

	def __str__(self):
		return f"{self.uid} - {self.email}"
