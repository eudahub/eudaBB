"""Forum (board) read/write endpoints."""

from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny

from board.models import Section, Board, Topic, Post, User
from api import response as R
from api.serializers import (
    SectionWithBoardsSerializer,
    TopicListSerializer,
    PostSerializer,
    PostWriteSerializer,
    NewTopicWriteSerializer,
    UserProfileSerializer,
)


def _can_access_board(board, user):
    """Return True if user may read this board."""
    if user and user.is_authenticated and (user.is_root or user.role >= User.ROLE_ADMIN):
        return True
    if board.access_level == Board.AccessLevel.PUBLIC:
        return True
    if board.access_level == Board.AccessLevel.REGISTERED:
        return user is not None and user.is_authenticated
    return False


def _get_readable_board(board_id, user):
    """Return Board or raise 404/403 response error."""
    board = get_object_or_404(Board, pk=board_id)
    if not _can_access_board(board, user):
        return None, R.error("FORBIDDEN", "Brak dostępu do tego działu.", 403)
    return board, None


# ---------------------------------------------------------------------------
# GET /api/v1/categories
# ---------------------------------------------------------------------------

class CategoriesView(APIView):
    """All sections with their boards (category tree)."""
    permission_classes = [AllowAny]

    def get(self, request):
        sections = Section.objects.prefetch_related("boards").order_by("order")
        data = SectionWithBoardsSerializer(
            sections, many=True, context={"request": request}
        ).data
        return R.ok(data)


# ---------------------------------------------------------------------------
# GET /api/v1/categories/{board_id}/threads
# ---------------------------------------------------------------------------

class ThreadListView(APIView):
    """Topics in a board, paginated."""
    permission_classes = [AllowAny]

    def get(self, request, board_id):
        board, err = _get_readable_board(board_id, request.user)
        if err:
            return err

        qs = (
            Topic.objects
            .filter(board=board)
            .select_related("author", "last_post")
            .order_by("-topic_type", "-last_post_at")
        )
        return R.paginate(qs, request, TopicListSerializer, per_page=30)


# ---------------------------------------------------------------------------
# GET /api/v1/threads/{topic_id}/posts
# ---------------------------------------------------------------------------

class PostListView(APIView):
    """Posts in a topic, paginated."""
    permission_classes = [AllowAny]

    def get(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        board, err = _get_readable_board(topic.board_id, request.user)
        if err:
            return err

        # Increment view count (fire-and-forget)
        Topic.objects.filter(pk=topic_id).update(view_count=topic.view_count + 1)

        qs = (
            Post.objects
            .filter(topic=topic)
            .select_related("author")
            .order_by("post_order")
        )
        return R.paginate(
            qs, request, PostSerializer,
            per_page=20, context={"request": request}
        )


# ---------------------------------------------------------------------------
# GET /api/v1/posts/{post_id}
# ---------------------------------------------------------------------------

class PostDetailView(APIView):
    """Single post by ID."""
    permission_classes = [AllowAny]

    def get(self, request, post_id):
        post = get_object_or_404(Post.objects.select_related("author"), pk=post_id)
        board, err = _get_readable_board(post.topic.board_id, request.user)
        if err:
            return err
        return R.ok(PostSerializer(post, context={"request": request}).data)


# ---------------------------------------------------------------------------
# POST /api/v1/threads  — create new topic
# ---------------------------------------------------------------------------

class CreateThreadView(APIView):
    """Create a new topic in a board."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        board_id = request.data.get("board_id")
        if not board_id:
            return R.error("MISSING_FIELD", "Wymagane pole: board_id.")

        board, err = _get_readable_board(board_id, request.user)
        if err:
            return err

        if board.access_level > Board.AccessLevel.REGISTERED:
            return R.error("FORBIDDEN", "Brak uprawnień do pisania w tym dziale.", 403)

        # Antiflood check
        from board.antiflood import check_can_post
        flood = check_can_post(request.user)
        if not flood["allowed"]:
            return R.error(
                "FLOOD_LIMIT",
                flood.get("message", "Zbyt wiele wpisów — odczekaj chwilę."),
                429,
            )

        ser = NewTopicWriteSerializer(data=request.data)
        if not ser.is_valid():
            return R.error("VALIDATION_ERROR", str(ser.errors))

        title = ser.validated_data["title"].strip()
        content = ser.validated_data["content"]

        topic = Topic.objects.create(
            board=board,
            title=title,
            author=request.user,
        )
        post = Post.objects.create(
            topic=topic,
            author=request.user,
            content_bbcode=content,
            post_order=1,
            author_ip=_get_client_ip(request),
        )
        from board.active_days import increment_if_new_day
        increment_if_new_day(request.user, post)
        # Update board and topic caches
        _update_topic_last_post(topic, post)
        _update_board_last_post(board, post)

        return R.created({
            "topic_id": topic.pk,
            "post_id": post.pk,
        })


# ---------------------------------------------------------------------------
# POST /api/v1/threads/{topic_id}/posts  — reply
# ---------------------------------------------------------------------------

class CreatePostView(APIView):
    """Post a reply in a topic."""
    permission_classes = [IsAuthenticated]

    def post(self, request, topic_id):
        topic = get_object_or_404(Topic, pk=topic_id)
        board, err = _get_readable_board(topic.board_id, request.user)
        if err:
            return err

        if topic.is_locked:
            if not (request.user.is_root or request.user.role >= User.ROLE_MODERATOR):
                return R.error("TOPIC_LOCKED", "Wątek jest zamknięty.", 403)

        # Antiflood check
        from board.antiflood import check_can_post
        flood = check_can_post(request.user)
        if not flood["allowed"]:
            return R.error(
                "FLOOD_LIMIT",
                flood.get("message", "Zbyt wiele wpisów — odczekaj chwilę."),
                429,
            )

        ser = PostWriteSerializer(data=request.data)
        if not ser.is_valid():
            return R.error("VALIDATION_ERROR", str(ser.errors))

        last_order = (
            Post.objects.filter(topic=topic).order_by("-post_order").values_list("post_order", flat=True).first()
            or 0
        )
        post = Post.objects.create(
            topic=topic,
            author=request.user,
            content_bbcode=ser.validated_data["content"],
            post_order=last_order + 1,
            author_ip=_get_client_ip(request),
        )
        from board.active_days import increment_if_new_day
        increment_if_new_day(request.user, post)
        _update_topic_last_post(topic, post)
        _update_board_last_post(topic.board, post)

        return R.created(PostSerializer(post, context={"request": request}).data)


# ---------------------------------------------------------------------------
# PUT /api/v1/posts/{post_id}  — edit own post
# ---------------------------------------------------------------------------

class EditPostView(APIView):
    """Edit your own post."""
    permission_classes = [IsAuthenticated]

    def put(self, request, post_id):
        post = get_object_or_404(Post, pk=post_id)

        if post.author_id != request.user.pk:
            return R.error("FORBIDDEN", "Możesz edytować tylko własne posty.", 403)

        ser = PostWriteSerializer(data=request.data)
        if not ser.is_valid():
            return R.error("VALIDATION_ERROR", str(ser.errors))

        post.content_bbcode = ser.validated_data["content"]
        post.updated_at = timezone.now()
        post.updated_by = request.user
        post.edit_count += 1
        post.save(update_fields=["content_bbcode", "updated_at", "updated_by", "edit_count"])

        return R.ok(PostSerializer(post, context={"request": request}).data)


# ---------------------------------------------------------------------------
# DELETE /api/v1/posts/{post_id}  — delete own post
# ---------------------------------------------------------------------------

class DeletePostView(APIView):
    """Delete your own post (moderators can delete any)."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, post_id):
        post = get_object_or_404(Post, pk=post_id)
        user = request.user

        is_mod = user.is_root or user.role >= User.ROLE_MODERATOR
        if post.author_id != user.pk and not is_mod:
            return R.error("FORBIDDEN", "Brak uprawnień do usunięcia tego posta.", 403)

        topic = post.topic
        from board.active_days import decrement_if_last_on_day
        if post.author:
            decrement_if_last_on_day(post.author, post)
        post.delete()

        # Refresh topic counters
        remaining = Post.objects.filter(topic=topic).count()
        if remaining == 0:
            topic.delete()
        else:
            topic.reply_count = max(0, remaining - 1)
            last = Post.objects.filter(topic=topic).order_by("-created_at").first()
            topic.last_post = last
            topic.last_post_at = last.created_at if last else None
            topic.save(update_fields=["reply_count", "last_post", "last_post_at"])

        return R.ok({"deleted": True})


# ---------------------------------------------------------------------------
# POST /api/v1/posts/{post_id}/report
# ---------------------------------------------------------------------------

class ReportPostView(APIView):
    """Report a post for moderation. Creates a PostReport record (stub)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, post_id):
        post = get_object_or_404(Post, pk=post_id)
        reason  = (request.data.get("reason")  or "").strip()
        comment = (request.data.get("comment") or "").strip()[:300]

        from board.report_utils import open_report
        report, created = open_report(post, request.user, reason, comment)
        if not created:
            return R.error("ALREADY_REPORTED", "Już zgłosiłeś tego posta.", 409)

        return R.created({"report_id": report.pk})


# ---------------------------------------------------------------------------
# GET /api/v1/users/{user_id}/profile
# ---------------------------------------------------------------------------

class UserProfileView(APIView):
    """Public user profile."""
    permission_classes = [AllowAny]

    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        return R.ok(UserProfileSerializer(user, context={"request": request}).data)


# ---------------------------------------------------------------------------
# GET /api/v1/search
# ---------------------------------------------------------------------------

class SearchView(APIView):
    """Simple text search in topic titles and post content.

    Query params: ?q=text&type=thread|post&page=1
    """
    permission_classes = [AllowAny]

    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        search_type = request.query_params.get("type", "thread")

        if len(q) < 2:
            return R.error("QUERY_TOO_SHORT", "Zapytanie musi mieć co najmniej 2 znaki.")

        user = request.user

        if search_type == "post":
            qs = (
                Post.objects
                .filter(content_bbcode__icontains=q)
                .select_related("author", "topic__board")
                .order_by("-created_at")
            )
            # Filter by board access
            accessible_board_ids = _accessible_board_ids(user)
            qs = qs.filter(topic__board_id__in=accessible_board_ids)
            return R.paginate(qs, request, PostSerializer, per_page=20, context={"request": request})
        else:
            qs = (
                Topic.objects
                .filter(title__icontains=q)
                .select_related("author")
                .order_by("-last_post_at")
            )
            accessible_board_ids = _accessible_board_ids(user)
            qs = qs.filter(board_id__in=accessible_board_ids)
            return R.paginate(qs, request, TopicListSerializer, per_page=20)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client_ip(request):
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _update_topic_last_post(topic, post):
    topic.reply_count = Post.objects.filter(topic=topic).count() - 1
    topic.last_post = post
    topic.last_post_at = post.created_at
    topic.save(update_fields=["reply_count", "last_post", "last_post_at"])


def _update_board_last_post(board, post):
    board.post_count = Post.objects.filter(topic__board=board).count()
    board.last_post = post
    board.last_post_at = post.created_at
    board.save(update_fields=["post_count", "last_post", "last_post_at"])


def _accessible_board_ids(user):
    if user and user.is_authenticated and (user.is_root or user.role >= User.ROLE_ADMIN):
        return list(Board.objects.values_list("pk", flat=True))
    if user and user.is_authenticated:
        return list(
            Board.objects.filter(access_level__lte=Board.AccessLevel.REGISTERED)
            .values_list("pk", flat=True)
        )
    return list(
        Board.objects.filter(access_level=Board.AccessLevel.PUBLIC)
        .values_list("pk", flat=True)
    )
