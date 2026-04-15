from django.urls import path

from api.views.auth import (
    Argon2ParamsView,
    RegisterInitView,
    RegisterView,
    LoginInitView,
    LoginView,
    TokenRefreshView,
    LogoutView,
    ResetRequestView,
    ResetConfirmView,
)
from api.views.forum import (
    CategoriesView,
    ThreadListView,
    PostListView,
    PostDetailView,
    CreateThreadView,
    CreatePostView,
    EditPostView,
    DeletePostView,
    ReportPostView,
    UserProfileView,
    SearchView,
)
from api.views.moderation import (
    ModDeletePostView,
    ModEditPostView,
    ModLockView,
    ModUnlockView,
    ModPinView,
    ModUnpinView,
    ModMoveThreadView,
    ModBanView,
    ModUnbanView,
    ModReportListView,
    ModReportResolveView,
    ModReportDismissView,
)
from api.views.push import PushRegisterView, PushUnregisterView
from api.views.pm import (
    ConversationListView,
    ConversationDetailView,
    SendPMView,
    ReplyPMView,
)
from api.views.notifications import (
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
)

app_name = "api"

urlpatterns = [
    # ---- Auth ----
    path("auth/argon2-params",     Argon2ParamsView.as_view(),  name="argon2_params"),
    path("auth/register-init",     RegisterInitView.as_view(),  name="register_init"),
    path("auth/register",          RegisterView.as_view(),       name="register"),
    path("auth/login-init",        LoginInitView.as_view(),      name="login_init"),
    path("auth/login",             LoginView.as_view(),          name="login"),
    path("auth/refresh",           TokenRefreshView.as_view(),   name="token_refresh"),
    path("auth/logout",            LogoutView.as_view(),         name="logout"),
    path("auth/reset-request",     ResetRequestView.as_view(),   name="reset_request"),
    path("auth/reset-confirm",     ResetConfirmView.as_view(),   name="reset_confirm"),

    # ---- Forum read ----
    path("categories",                           CategoriesView.as_view(),       name="categories"),
    path("categories/<int:forum_id>/threads",    ThreadListView.as_view(),       name="thread_list"),
    path("threads/<int:topic_id>/posts",         PostListView.as_view(),         name="post_list"),
    path("posts/<int:post_id>",                  PostDetailView.as_view(),       name="post_detail"),
    path("users/<int:user_id>/profile",          UserProfileView.as_view(),      name="user_profile"),
    path("search",                               SearchView.as_view(),            name="search"),

    # ---- Forum write ----
    path("threads",                              CreateThreadView.as_view(),     name="create_thread"),
    path("threads/<int:topic_id>/posts",         CreatePostView.as_view(),       name="create_post"),
    path("posts/<int:post_id>",                  EditPostView.as_view(),         name="edit_post"),
    path("posts/<int:post_id>/delete",           DeletePostView.as_view(),       name="delete_post"),
    path("posts/<int:post_id>/report",           ReportPostView.as_view(),       name="report_post"),

    # ---- Moderation ----
    path("mod/posts/<int:post_id>",              ModDeletePostView.as_view(),    name="mod_delete_post"),
    path("mod/posts/<int:post_id>/edit",         ModEditPostView.as_view(),      name="mod_edit_post"),
    path("mod/threads/<int:topic_id>/lock",      ModLockView.as_view(),          name="mod_lock"),
    path("mod/threads/<int:topic_id>/unlock",    ModUnlockView.as_view(),        name="mod_unlock"),
    path("mod/threads/<int:topic_id>/pin",       ModPinView.as_view(),           name="mod_pin"),
    path("mod/threads/<int:topic_id>/unpin",     ModUnpinView.as_view(),         name="mod_unpin"),
    path("mod/threads/<int:topic_id>/move",      ModMoveThreadView.as_view(),    name="mod_move"),
    path("mod/users/<int:user_id>/ban",          ModBanView.as_view(),           name="mod_ban"),
    path("mod/users/<int:user_id>/ban/lift",     ModUnbanView.as_view(),         name="mod_unban"),
    path("mod/reports",                          ModReportListView.as_view(),    name="mod_reports"),
    path("mod/reports/<int:report_id>/resolve",  ModReportResolveView.as_view(), name="mod_report_resolve"),
    path("mod/reports/<int:report_id>/dismiss",  ModReportDismissView.as_view(), name="mod_report_dismiss"),

    # ---- Push notifications (FCM stub) ----
    path("push/register",    PushRegisterView.as_view(),   name="push_register"),
    path("push/unregister",  PushUnregisterView.as_view(), name="push_unregister"),

    # ---- Private messages ----
    path("conversations",                     ConversationListView.as_view(),   name="conversation_list"),
    path("conversations/<int:box_id>",        ConversationDetailView.as_view(), name="conversation_detail"),
    path("conversations/new",                 SendPMView.as_view(),             name="send_pm"),
    path("conversations/<int:box_id>/reply",  ReplyPMView.as_view(),            name="reply_pm"),

    # ---- Notifications (polling stub) ----
    path("notifications",                          NotificationListView.as_view(),     name="notification_list"),
    path("notifications/<int:notification_id>/read", NotificationMarkReadView.as_view(), name="notification_read"),
    path("notifications/read-all",                 NotificationMarkAllReadView.as_view(), name="notification_read_all"),
]
