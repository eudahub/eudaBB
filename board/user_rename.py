import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import Post, QuoteReference, User
from .quote_refs import rebuild_quote_references_for_posts
from .user_lock import user_processing_lock
from .username_utils import normalize


_ANY_ENRICHED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)(?:="(?P<author>[^"]*)")?(?P<mid>\s+post_id=(?P<post_id>\d+))(?P<tail>[^\]]*)\]',
    re.IGNORECASE,
)

_NAMED_QUOTE_RE = re.compile(
    r'\[(?P<qtype>f?quote)="(?P<author>[^"]*)"\]',
    re.IGNORECASE,
)


def validate_new_username(user: User, new_username: str) -> str:
    new_username = (new_username or "").strip()
    if not new_username:
        raise ValidationError("Nowa nazwa użytkownika nie może być pusta.")

    if new_username == user.username:
        raise ValidationError("To już jest aktualna nazwa tego konta.")

    norm = normalize(new_username)
    if not norm:
        raise ValidationError(
            "Nowa nazwa po normalizacji jest pusta. Użyj liter lub cyfr."
        )

    exact_conflict = User.objects.filter(username=new_username).exclude(pk=user.pk).first()
    if exact_conflict:
        raise ValidationError(
            f"Nazwa użytkownika '{new_username}' już istnieje."
        )

    normalized_conflict = (
        User.objects.filter(username_normalized=norm)
        .exclude(pk=user.pk)
        .first()
    )
    if normalized_conflict:
        raise ValidationError(
            f"Nazwa po normalizacji koliduje z istniejącym kontem "
            f"'{normalized_conflict.username}'."
        )

    return new_username


def _rewrite_named_quotes_only(content: str, old_username: str, new_username: str):
    changed = 0

    def repl(match):
        nonlocal changed
        if match.group("author") != old_username:
            return match.group(0)
        changed += 1
        return f'[{match.group("qtype")}="{new_username}"]'

    return _NAMED_QUOTE_RE.sub(repl, content), changed


def _rewrite_enriched_quotes(content: str, old_username: str, new_username: str, source_post_ids: frozenset[int]):
    changed = 0

    def repl(match):
        nonlocal changed
        author = (match.group("author") or "").strip()
        source_post_id = int(match.group("post_id"))

        # Update either explicit quotes of the old username or quotes pointing
        # at posts authored by the renamed user.
        if author != old_username and source_post_id not in source_post_ids:
            return match.group(0)

        changed += 1
        return '[%s="%s"%s%s]' % (
            match.group("qtype"),
            new_username,
            match.group("mid"),
            match.group("tail") or "",
        )

    return _ANY_ENRICHED_QUOTE_RE.sub(repl, content), changed


def rename_user_and_update_quotes(user: User, new_username: str) -> dict:
    new_username = validate_new_username(user, new_username)
    old_username = user.username
    source_post_ids = frozenset(Post.objects.filter(author=user).values_list("pk", flat=True))

    quote_post_ids = list(
        QuoteReference.objects.filter(
            Q(quoted_username=old_username) |
            Q(source_post__author=user)
        )
        .values_list("post_id", flat=True)
        .distinct()
    )
    posts_qs = Post.objects.filter(pk__in=quote_post_ids).only("pk", "content_bbcode").order_by("pk")

    posts_changed = 0
    tags_changed = 0
    batch = []
    changed_post_ids = []

    with user_processing_lock(user), transaction.atomic():
        for post in posts_qs.iterator(chunk_size=500):
            updated_content, changed_named = _rewrite_named_quotes_only(
                post.content_bbcode, old_username, new_username
            )
            updated_content, changed_enriched = _rewrite_enriched_quotes(
                updated_content, old_username, new_username, source_post_ids
            )
            changed_total = changed_named + changed_enriched
            if not changed_total:
                continue

            post.content_bbcode = updated_content
            batch.append(post)
            changed_post_ids.append(post.pk)
            posts_changed += 1
            tags_changed += changed_total

            if len(batch) >= 500:
                Post.objects.bulk_update(batch, ["content_bbcode"])
                batch.clear()

        if batch:
            Post.objects.bulk_update(batch, ["content_bbcode"])

        if changed_post_ids:
            rebuild_quote_references_for_posts(
                Post.objects.filter(pk__in=changed_post_ids).only("pk", "content_bbcode")
            )

        user.username = new_username
        user.save(update_fields=["username"])

    return {
        "old_username": old_username,
        "new_username": new_username,
        "posts_changed": posts_changed,
        "tags_changed": tags_changed,
    }
