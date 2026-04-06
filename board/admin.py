from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from .models import User, Section, Forum, Topic, Post


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "email", "post_count", "is_ghost", "is_active", "is_banned", "is_staff"]
    list_filter = ["is_ghost", "is_active", "is_banned", "is_staff"]
    actions = ["activate_accounts", "delete_empty_accounts"]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Forum profile", {"fields": ("signature", "website", "location", "avatar", "post_count", "rank", "is_ghost", "is_banned", "ban_reason", "archive_access")}),
    )

    @admin.action(description="Aktywuj wybrane konta (is_ghost=False, is_active=True)")
    def activate_accounts(self, request, queryset):
        updated = queryset.filter(is_ghost=True).update(is_ghost=False, is_active=True)
        self.message_user(request, f"Aktywowano {updated} kont.", messages.SUCCESS)

    @admin.action(description="Usuń puste konta (bez postów, tematów i PM)")
    def delete_empty_accounts(self, request, queryset):
        selected_count = queryset.count()
        deletable = queryset.filter(
            is_root=False,
            posts__isnull=True,
            topics__isnull=True,
            pm_boxes__isnull=True,
            sent_pms__isnull=True,
            received_pms__isnull=True,
        ).distinct()
        deleted_count = deletable.count()
        deletable.delete()
        skipped = selected_count - deleted_count
        self.message_user(
            request,
            f"Usunięto {deleted_count} pustych kont. Pominięto {skipped}.",
            messages.SUCCESS if deleted_count else messages.WARNING,
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
