from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from .models import User, Section, Forum, Topic, Post, SiteConfig, MaintenanceAllowedUser


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


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ["site_mode", "reset_mode", "show_switch_link"]
    fieldsets = [
        ("Tryb działania", {"fields": ["site_mode", "maintenance_message"]}),
        ("Pozostałe", {"fields": ["reset_mode", "show_switch_link", "search_snippet_chars", "poll_options_soft_max"]}),
    ]

    def _is_root(self, request):
        return getattr(request.user, "is_root", False)

    def has_module_perms(self, request):
        return self._is_root(request)

    def has_view_permission(self, request, obj=None):
        return self._is_root(request)

    def has_add_permission(self, request):
        return self._is_root(request) and not SiteConfig.objects.exists()

    def has_change_permission(self, request, obj=None):
        return self._is_root(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MaintenanceAllowedUser)
class MaintenanceAllowedUserAdmin(admin.ModelAdmin):
    list_display = ["username", "added_at"]
    search_fields = ["username"]

    def _is_root(self, request):
        return getattr(request.user, "is_root", False)

    def has_module_perms(self, request):
        return self._is_root(request)

    def has_view_permission(self, request, obj=None):
        return self._is_root(request)

    def has_add_permission(self, request):
        return self._is_root(request)

    def has_change_permission(self, request, obj=None):
        return self._is_root(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_root(request)
