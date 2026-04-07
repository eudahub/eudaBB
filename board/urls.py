from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("reset-hasla/", views.request_reset, name="request_reset"),
    path("ustaw-haslo/", views.do_reset, name="do_reset"),
    path("", views.index, name="index"),
    path("forum/<int:forum_id>/", views.forum_detail, name="forum_detail"),
    path("topic/<int:topic_id>/", views.topic_detail, name="topic_detail"),
    path("forum/<int:forum_id>/new/", views.new_topic, name="new_topic"),
    path("topic/<int:topic_id>/reply/", views.reply, name="reply"),
    path("topic/<int:topic_id>/preview/", views.preview_post, name="preview_post"),
    path("post/<int:post_id>/quote-fragment/", views.quote_fragment, name="quote_fragment"),
    path("register/", views.register, name="register"),
    path("activate-ghost/", views.activate_ghost, name="activate_ghost"),
    path("activate/<str:token>/", views.activate_confirm, name="activate_confirm"),
    path("znajdz-konto/", views.find_account, name="find_account"),
    path("kontakt/", views.contact, name="contact"),
    path("szukaj/", views.search, name="search"),
    path("admin/blocked-ips/", views.admin_blocked_ips, name="admin_blocked_ips"),
    path("post/<int:post_id>/flag-ip/", views.flag_post_ip, name="flag_post_ip"),
    path("root/config/", views.root_config, name="root_config"),
    path("post/<int:post_id>/", views.goto_post, name="goto_post"),
    # Private Messages
    path("pm/",                     views.pm_inbox,   name="pm_inbox"),
    path("pm/outbox/",              views.pm_outbox,  name="pm_outbox"),
    path("pm/sent/",                views.pm_sent,    name="pm_sent"),
    path("pm/compose/",             views.pm_compose, name="pm_compose"),
    path("pm/<int:box_id>/",        views.pm_view,    name="pm_view"),
    path("pm/<int:box_id>/edit/",   views.pm_edit,    name="pm_edit"),
    path("pm/<int:box_id>/delete/", views.pm_delete,  name="pm_delete"),
]
