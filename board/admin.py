from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, Section, Forum, Topic, Post


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "email", "post_count", "is_banned", "is_staff"]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Forum profile", {"fields": ("signature", "website", "location", "avatar", "post_count", "rank", "is_banned", "ban_reason", "archive_access")}),
    )


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    list_display = ["title", "order"]


@admin.register(Forum)
class ForumAdmin(admin.ModelAdmin):
    list_display = ["title", "section", "parent", "order", "topic_count", "post_count", "access_level", "archive_level"]
    list_filter = ["access_level", "archive_level", "section"]


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ["title", "forum", "author", "topic_type", "is_locked", "reply_count", "view_count", "created_at"]
    list_filter = ["forum", "topic_type", "is_locked"]
    search_fields = ["title"]


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ["__str__", "author", "topic", "created_at", "post_order"]
    list_filter = ["topic__forum"]
    search_fields = ["content_bbcode", "author__username"]
