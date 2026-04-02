from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("forum/<int:forum_id>/", views.forum_detail, name="forum_detail"),
    path("topic/<int:topic_id>/", views.topic_detail, name="topic_detail"),
    path("forum/<int:forum_id>/new/", views.new_topic, name="new_topic"),
    path("topic/<int:topic_id>/reply/", views.reply, name="reply"),
    path("register/", views.register, name="register"),
    path("activate-ghost/", views.activate_ghost, name="activate_ghost"),
    path("activate/<str:token>/", views.activate_confirm, name="activate_confirm"),
    path("znajdz-konto/", views.find_account, name="find_account"),
]
