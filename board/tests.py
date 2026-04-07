from django.core.exceptions import ValidationError
from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from .models import Forum, Poll, PollOption, PollVote, PostLike, Section, Topic, User, Post, TopicParticipant, TopicReadState
from .quote_refs import rebuild_quote_references_for_post, rebuild_quote_references_for_posts
from .quote_selection import extract_exact_quote_fragment
from .quote_validation import validate_enriched_quotes
from .forms import NewTopicForm, TOPIC_TITLE_MAX_LENGTH, validate_pm_content, validate_post_content
from .polls import parse_poll_results_text, validate_poll_option_count
from .bbcode_lint import repair as repair_bbcode
from .search_index import extract_author_search_text, rebuild_post_search_index_for_posts
from .user_rename import rename_user_and_update_quotes
from .username_utils import normalize
from .views import _build_search_snippet, _matches_search_text, _parse_search_query


class UserRenameTests(TestCase):
    def setUp(self):
        self.section = Section.objects.create(title="Sekcja", order=1)
        self.forum = Forum.objects.create(
            section=self.section,
            title="Forum",
            description="",
            order=1,
        )

    def _make_topic(self, author: User, title: str = "Temat") -> Topic:
        return Topic.objects.create(forum=self.forum, title=title, author=author)

    def test_rename_updates_username_normalized_and_quotes(self):
        renamed_user = User.objects.create_user(username="Stary Łoś", password="x")
        other_user = User.objects.create_user(username="Inny", password="x")
        topic = self._make_topic(other_user)

        source_post = Post.objects.create(
            topic=topic,
            author=renamed_user,
            content_bbcode="Źródło",
            post_order=1,
        )
        named_quote_post = Post.objects.create(
            topic=topic,
            author=other_user,
            content_bbcode='[quote="Stary Łoś"]abc[/quote]',
            post_order=2,
        )
        enriched_quote_post = Post.objects.create(
            topic=topic,
            author=other_user,
            content_bbcode=f'[quote="Ktoś" post_id={source_post.pk}]abc[/quote]',
            post_order=3,
        )
        foreign_quote_post = Post.objects.create(
            topic=topic,
            author=other_user,
            content_bbcode=f'[fquote="Stary Łoś" post_id={source_post.pk}]abc[/fquote]',
            post_order=4,
        )
        rebuild_quote_references_for_posts(
            Post.objects.filter(pk__in=[
                named_quote_post.pk, enriched_quote_post.pk, foreign_quote_post.pk
            ]).only("pk", "content_bbcode")
        )

        result = rename_user_and_update_quotes(renamed_user, "Nowy Żubr")

        renamed_user.refresh_from_db()
        named_quote_post.refresh_from_db()
        enriched_quote_post.refresh_from_db()
        foreign_quote_post.refresh_from_db()

        self.assertEqual(renamed_user.username, "Nowy Żubr")
        self.assertEqual(renamed_user.username_normalized, normalize("Nowy Żubr"))
        self.assertEqual(result["posts_changed"], 3)
        self.assertEqual(result["tags_changed"], 3)
        self.assertIn('[quote="Nowy Żubr"]', named_quote_post.content_bbcode)
        self.assertIn('[quote="Nowy Żubr" post_id=', enriched_quote_post.content_bbcode)
        self.assertIn('[fquote="Nowy Żubr" post_id=', foreign_quote_post.content_bbcode)

    def test_rename_rejects_normalized_collision(self):
        renamed_user = User.objects.create_user(username="Stary", password="x")
        User.objects.create_user(username="Łukasz", password="x")

        with self.assertRaises(ValidationError):
            rename_user_and_update_quotes(renamed_user, "Lukasz")

    def test_rename_allows_same_normalized_name_for_same_user(self):
        renamed_user = User.objects.create_user(username="Andy72", password="x")

        result = rename_user_and_update_quotes(renamed_user, "andy72")

        renamed_user.refresh_from_db()
        self.assertEqual(result["old_username"], "Andy72")
        self.assertEqual(result["new_username"], "andy72")
        self.assertEqual(renamed_user.username, "andy72")
        self.assertEqual(renamed_user.username_normalized, "andy72")

    def test_root_config_can_rename_user(self):
        root = User.objects.create_user(
            username="root",
            password="x",
            is_root=True,
            is_staff=True,
            is_superuser=True,
        )
        renamed_user = User.objects.create_user(username="AdminX", password="x", is_staff=True)
        other_user = User.objects.create_user(username="Inny", password="x")
        topic = self._make_topic(other_user)
        source_post = Post.objects.create(
            topic=topic,
            author=renamed_user,
            content_bbcode="Źródło",
            post_order=1,
        )
        quoting_post = Post.objects.create(
            topic=topic,
            author=other_user,
            content_bbcode=f'[quote="Błędny" post_id={source_post.pk}]abc[/quote]',
            post_order=2,
        )
        rebuild_quote_references_for_post(quoting_post)

        client = Client()
        client.force_login(root)
        response = client.post(
            reverse("root_config"),
            {
                "action": "rename_user",
                "rename_user_id": str(renamed_user.pk),
                "new_username": "AdminY",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        renamed_user.refresh_from_db()
        quoting_post.refresh_from_db()
        self.assertEqual(renamed_user.username, "AdminY")
        self.assertIn('[quote="AdminY" post_id=', quoting_post.content_bbcode)

    def test_quote_refs_capture_nested_depth(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='[quote="A"]x[quote="B"]y[/quote][/quote]',
            post_order=1,
        )

        rebuild_quote_references_for_post(post)
        refs = list(post.quote_references.order_by("quote_index").values_list("quoted_username", "depth"))

        self.assertEqual(refs, [("A", 1), ("B", 2)])

    def test_topic_detail_renders_quote_button(self):
        author = User.objects.create_user(username="Autor", password="x")
        reader = User.objects.create_user(username="Czytelnik", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Treść posta",
            post_order=1,
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cytuj")
        self.assertContains(response, f'data-post-id="{post.pk}"')
        self.assertContains(response, 'data-post-content="1"')

    def test_topic_detail_renders_like_button_for_other_users_post(self):
        author = User.objects.create_user(username="AutorLike1", password="x")
        reader = User.objects.create_user(username="CzytelnikLike1", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="Treść", post_order=1)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("toggle_post_like", args=[post.pk]))
        self.assertContains(response, "Polubienia: 0")

    def test_toggle_post_like_adds_like_and_updates_counters(self):
        author = User.objects.create_user(username="AutorLike2", password="x")
        reader = User.objects.create_user(username="CzytelnikLike2", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="Treść", post_order=1)

        client = Client()
        client.force_login(reader)
        response = client.post(
            reverse("toggle_post_like", args=[post.pk]),
            {"next": reverse("topic_detail", args=[topic.pk])},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PostLike.objects.filter(post=post, user=reader).exists())
        post.refresh_from_db()
        author.refresh_from_db()
        reader.refresh_from_db()
        self.assertEqual(post.like_count, 1)
        self.assertEqual(author.likes_received_count, 1)
        self.assertEqual(reader.likes_given_count, 1)

    def test_toggle_post_like_removes_existing_like(self):
        author = User.objects.create_user(username="AutorLike3", password="x")
        reader = User.objects.create_user(username="CzytelnikLike3", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="Treść", post_order=1)
        PostLike.objects.create(post=post, user=reader)

        client = Client()
        client.force_login(reader)
        response = client.post(
            reverse("toggle_post_like", args=[post.pk]),
            {"next": reverse("topic_detail", args=[topic.pk])},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PostLike.objects.filter(post=post, user=reader).exists())
        post.refresh_from_db()
        author.refresh_from_db()
        reader.refresh_from_db()
        self.assertEqual(post.like_count, 0)
        self.assertEqual(author.likes_received_count, 0)
        self.assertEqual(reader.likes_given_count, 0)

    def test_toggle_post_like_rejects_own_post(self):
        author = User.objects.create_user(username="AutorLike4", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="Treść", post_order=1)

        client = Client()
        client.force_login(author)
        response = client.post(
            reverse("toggle_post_like", args=[post.pk]),
            {"next": reverse("topic_detail", args=[topic.pk])},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PostLike.objects.filter(post=post, user=author).exists())
        post.refresh_from_db()
        self.assertEqual(post.like_count, 0)

    def test_user_likes_views_show_received_and_given(self):
        author = User.objects.create_user(username="AutorLike5", password="x")
        giver = User.objects.create_user(username="CzytelnikLike5", password="x")
        topic = self._make_topic(author, title="Temat polubień")
        post = Post.objects.create(topic=topic, author=author, content_bbcode="Treść", post_order=1)
        PostLike.objects.create(post=post, user=giver)

        received = self.client.get(reverse("user_likes_received", args=[author.pk]))
        self.assertEqual(received.status_code, 200)
        self.assertContains(received, "Polubienia otrzymane")
        self.assertContains(received, giver.username)
        self.assertContains(received, topic.title)

        given = self.client.get(reverse("user_likes_given", args=[giver.pk]))
        self.assertEqual(given.status_code, 200)
        self.assertContains(given, "Polubienia dane")
        self.assertContains(given, author.username)
        self.assertContains(given, topic.title)

    def test_reply_view_renders_quote_picker_and_recent_post_buttons(self):
        author = User.objects.create_user(username="Autor", password="x")
        reader = User.objects.create_user(username="Czytelnik", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Treść posta",
            post_order=1,
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("reply", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="enter-quote-picker"', html=False)
        self.assertContains(response, 'id="quote-picker-confirm"', html=False)
        self.assertContains(response, 'id="recent-posts-panel"', html=False)
        self.assertContains(response, 'data-quote-post="1"', html=False)
        self.assertContains(response, f'data-post-id="{post.pk}"', html=False)

    def test_reply_view_paginates_recent_posts_for_quote_picker(self):
        author = User.objects.create_user(username="Autor2", password="x")
        reader = User.objects.create_user(username="Czytelnik2", password="x")
        topic = self._make_topic(author)
        forum = topic.forum
        posts_per_page = getattr(settings, "POSTS_PER_PAGE", 20)

        for order in range(1, posts_per_page + 3):
            Post.objects.create(
                topic=topic,
                author=author,
                content_bbcode=f"Treść {order}",
                post_order=order,
            )
        topic.last_post = topic.posts.order_by("-post_order").first()
        topic.reply_count = topic.posts.count() - 1
        topic.save(update_fields=["last_post", "reply_count"])
        forum.last_post = topic.last_post
        forum.post_count = topic.posts.count()
        forum.topic_count = 1
        forum.save(update_fields=["last_post", "post_count", "topic_count"])

        client = Client()
        client.force_login(reader)

        response_page_1 = client.get(reverse("reply", args=[topic.pk]))
        self.assertEqual(response_page_1.status_code, 200)
        self.assertContains(response_page_1, "Strona 1 / 2")
        self.assertContains(response_page_1, "?quotes_page=2")
        self.assertContains(response_page_1, f"Treść {posts_per_page + 2}")
        self.assertNotContains(response_page_1, "Treść 1")

        response_page_2 = client.get(reverse("reply", args=[topic.pk]), {"quotes_page": 2})
        self.assertEqual(response_page_2.status_code, 200)
        self.assertContains(response_page_2, "Strona 2 / 2")
        self.assertContains(response_page_2, "?quotes_page=1")
        self.assertContains(response_page_2, "Treść 1")

    def test_reply_view_renders_global_pinned_topic_first_posts(self):
        author = User.objects.create_user(username="Autor3", password="x")
        reader = User.objects.create_user(username="Czytelnik3", password="x")
        topic = self._make_topic(author, title="Bieżący temat")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść bieżąca", post_order=1)

        other_forum = Forum.objects.create(
            section=self.section,
            title="Ogłoszenia",
            description="",
            order=2,
        )
        sticky_topic = Topic.objects.create(
            forum=other_forum,
            title="Regulamin",
            author=author,
            topic_type=Topic.TopicType.STICKY,
        )
        pinned_post = Post.objects.create(
            topic=sticky_topic,
            author=author,
            content_bbcode="Treść przypięta",
            post_order=1,
        )
        sticky_topic.last_post = pinned_post
        sticky_topic.last_post_at = pinned_post.created_at
        sticky_topic.save(update_fields=["last_post", "last_post_at"])

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("reply", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="toggle-pinned-posts"', html=False)
        self.assertContains(response, 'class="toggle-pinned-post"', html=False)
        self.assertContains(response, "Regulamin")
        self.assertContains(response, "Ogłoszenia")
        self.assertContains(response, "Treść przypięta")
        self.assertContains(response, f'data-post-id="{pinned_post.pk}"', html=False)

    def test_reply_view_filters_quote_picker_by_author(self):
        author = User.objects.create_user(username="Autor4", password="x")
        other = User.objects.create_user(username="Inny4", password="x")
        reader = User.objects.create_user(username="Czytelnik4", password="x")
        topic = self._make_topic(author, title="Filtrowanie")
        post_a = Post.objects.create(topic=topic, author=author, content_bbcode="Treść autora", post_order=1)
        Post.objects.create(topic=topic, author=other, content_bbcode="Treść innego", post_order=2)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("reply", args=[topic.pk]), {"quote_author": author.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Treść autora")
        self.assertNotContains(response, "Treść innego")
        self.assertContains(response, f'data-post-id="{post_a.pk}"', html=False)

    def test_reply_view_filters_quote_picker_by_text(self):
        author = User.objects.create_user(username="Autor5", password="x")
        reader = User.objects.create_user(username="Czytelnik5", password="x")
        topic = self._make_topic(author, title="Filtrowanie tekstu")
        Post.objects.create(topic=topic, author=author, content_bbcode="Ala ma kota", post_order=1)
        Post.objects.create(topic=topic, author=author, content_bbcode="Pies ma budę", post_order=2)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("reply", args=[topic.pk]), {"quote_q": "kota"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ala ma kota")
        self.assertNotContains(response, "Pies ma budę")

    def test_new_posts_view_orders_latest_first(self):
        author = User.objects.create_user(username="Autor6", password="x")
        topic = self._make_topic(author, title="Nowe posty")
        older = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Starszy post",
            post_order=1,
        )
        newer = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Nowszy post",
            post_order=2,
        )

        response = self.client.get(reverse("new_posts"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.find("Nowszy post"), content.find("Starszy post"))
        self.assertContains(response, f'<a href="/post/{newer.pk}/">{topic.title}</a>', html=False)
        self.assertContains(response, f'<a href="/post/{older.pk}/">{topic.title}</a>', html=False)

    def test_new_posts_view_snippet_skips_quote_like_blocks(self):
        author = User.objects.create_user(username="Autor7", password="x")
        topic = self._make_topic(author, title="Nowe posty 2")
        Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='Własny tekst [quote="X"]ukryj[/quote] dalej',
            post_order=1,
        )

        response = self.client.get(reverse("new_posts"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Własny tekst dalej")
        self.assertNotContains(response, "ukryj")

    def test_new_topics_view_shows_topic_metadata(self):
        author = User.objects.create_user(username="Autor8", password="x")
        other = User.objects.create_user(username="Inny8", password="x")
        topic = self._make_topic(author, title="Świeży wątek")
        Post.objects.create(topic=topic, author=author, content_bbcode="Start", post_order=1)
        last_post = Post.objects.create(topic=topic, author=other, content_bbcode="Odpowiedź", post_order=2)
        topic.reply_count = 1
        topic.last_post = last_post
        topic.last_post_at = last_post.created_at
        topic.save(update_fields=["reply_count", "last_post", "last_post_at"])

        response = self.client.get(reverse("new_topics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Świeży wątek")
        self.assertContains(response, "autor pierwszego postu: Autor8")
        self.assertContains(response, "autor ostatniego postu: Inny8")
        self.assertContains(response, "postów: 2")

    def test_unanswered_topics_view_lists_only_topics_without_replies(self):
        author = User.objects.create_user(username="AutorUnanswered", password="x")
        unanswered = self._make_topic(author, title="Bez odpowiedzi")
        answered = self._make_topic(author, title="Z odpowiedzią")

        unanswered_first = Post.objects.create(topic=unanswered, author=author, content_bbcode="Start", post_order=1)
        answered_first = Post.objects.create(topic=answered, author=author, content_bbcode="Start", post_order=1)
        answered_reply = Post.objects.create(topic=answered, author=author, content_bbcode="Odpowiedź", post_order=2)

        unanswered.last_post = unanswered_first
        unanswered.last_post_at = unanswered_first.created_at
        unanswered.reply_count = 0
        unanswered.save(update_fields=["last_post", "last_post_at", "reply_count"])

        answered.last_post = answered_reply
        answered.last_post_at = answered_reply.created_at
        answered.reply_count = 1
        answered.save(update_fields=["last_post", "last_post_at", "reply_count"])

        response = self.client.get(reverse("unanswered_topics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bez odpowiedzi")
        self.assertNotContains(response, "Z odpowiedzią")
        self.assertContains(response, "postów: 1")

    def test_topic_detail_marks_current_page_as_read(self):
        author = User.objects.create_user(username="AutorRead", password="x")
        reader = User.objects.create_user(username="CzytelnikRead", password="x")
        topic = self._make_topic(author, title="Czytanie")
        for order in range(1, 26):
            post = Post.objects.create(topic=topic, author=author, content_bbcode=f"Post {order}", post_order=order)
            if order == 25:
                topic.last_post = post
                topic.last_post_at = post.created_at
        topic.reply_count = 24
        topic.save(update_fields=["last_post", "last_post_at", "reply_count"])

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]), {"page": 2})

        self.assertEqual(response.status_code, 200)
        state = TopicReadState.objects.get(user=reader, topic=topic)
        self.assertEqual(state.last_read_post_order, 25)

    def test_forum_detail_marks_unread_topics_and_links_to_first_unread(self):
        author = User.objects.create_user(username="AutorForumUnread", password="x")
        reader = User.objects.create_user(username="CzytelnikForumUnread", password="x")
        topic = self._make_topic(author, title="Forum nieprzeczytane")
        first_unread = None
        last_post = None
        for order in range(1, 26):
            last_post = Post.objects.create(topic=topic, author=author, content_bbcode=f"Post {order}", post_order=order)
            if order == 21:
                first_unread = last_post
        topic.last_post = last_post
        topic.last_post_at = last_post.created_at
        topic.reply_count = 24
        topic.save(update_fields=["last_post", "last_post_at", "reply_count"])
        TopicReadState.objects.create(user=reader, topic=topic, last_read_post_order=20)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("forum_detail", args=[self.forum.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Forum nieprzeczytane")
        self.assertContains(response, "[nowe]")
        self.assertContains(response, f'/topic/{topic.pk}/?page=2#post-{first_unread.pk}')

    def test_unread_topics_view_links_to_first_unread_page(self):
        author = User.objects.create_user(username="AutorUnread", password="x")
        reader = User.objects.create_user(username="CzytelnikUnread", password="x")
        topic = self._make_topic(author, title="Nieprzeczytany długi wątek")
        last_post = None
        first_unread = None
        for order in range(1, 26):
            last_post = Post.objects.create(topic=topic, author=author, content_bbcode=f"Post {order}", post_order=order)
            if order == 21:
                first_unread = last_post
        topic.last_post = last_post
        topic.last_post_at = last_post.created_at
        topic.reply_count = 24
        topic.save(update_fields=["last_post", "last_post_at", "reply_count"])
        TopicReadState.objects.create(user=reader, topic=topic, last_read_post_order=20)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("unread_topics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nieprzeczytany długi wątek")
        self.assertContains(response, f'/topic/{topic.pk}/?page=2#post-{first_unread.pk}')

    def test_new_topics_marks_unread_topics_and_links_to_first_unread(self):
        author = User.objects.create_user(username="AutorNewTopicsUnread", password="x")
        reader = User.objects.create_user(username="CzytelnikNewTopicsUnread", password="x")
        topic = self._make_topic(author, title="Nowy nieprzeczytany wątek")
        first_unread = None
        last_post = None
        for order in range(1, 26):
            last_post = Post.objects.create(topic=topic, author=author, content_bbcode=f"Post {order}", post_order=order)
            if order == 21:
                first_unread = last_post
        topic.last_post = last_post
        topic.last_post_at = last_post.created_at
        topic.reply_count = 24
        topic.save(update_fields=["last_post", "last_post_at", "reply_count"])
        TopicReadState.objects.create(user=reader, topic=topic, last_read_post_order=20)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("new_topics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nowy nieprzeczytany wątek")
        self.assertContains(response, "[nowe]")
        self.assertContains(response, f'/topic/{topic.pk}/?page=2#post-{first_unread.pk}')

    def test_mark_all_topics_read_sets_user_baseline_and_clears_states(self):
        author = User.objects.create_user(username="AutorMarkRead", password="x")
        reader = User.objects.create_user(username="CzytelnikMarkRead", password="x")
        topic = self._make_topic(author, title="Do oznaczenia")
        last_post = Post.objects.create(topic=topic, author=author, content_bbcode="Start", post_order=1)
        topic.last_post = last_post
        topic.last_post_at = last_post.created_at
        topic.reply_count = 0
        topic.save(update_fields=["last_post", "last_post_at", "reply_count"])
        TopicReadState.objects.create(user=reader, topic=topic, last_read_post_order=0)
        before = reader.mark_all_read_at

        client = Client()
        client.force_login(reader)
        response = client.post(reverse("mark_all_topics_read"))

        self.assertEqual(response.status_code, 302)
        reader.refresh_from_db()
        self.assertGreaterEqual(reader.mark_all_read_at, before)
        self.assertFalse(TopicReadState.objects.filter(user=reader).exists())

    def test_topic_detail_shows_topic_participants_with_post_counts(self):
        author = User.objects.create_user(username="AutorParticipants", password="x")
        other = User.objects.create_user(username="InnyParticipants", password="x")
        topic = self._make_topic(author, title="Uczestnicy")
        Post.objects.create(topic=topic, author=author, content_bbcode="Start", post_order=1)
        Post.objects.create(topic=topic, author=author, content_bbcode="Drugi", post_order=2)
        Post.objects.create(topic=topic, author=other, content_bbcode="Trzeci", post_order=3)

        response = self.client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Uczestnicy:")
        self.assertContains(response, "AutorParticipants (2)")
        self.assertContains(response, "InnyParticipants (1)")
        self.assertTrue(TopicParticipant.objects.filter(topic=topic, user=author, post_count=2).exists())

    def test_search_topics_mode_matches_title_and_marks_poll_topics(self):
        reader = User.objects.create_user(username="CzytelnikSearch1", password="x")
        author = User.objects.create_user(username="AutorSearch1", password="x")
        matching = self._make_topic(author, title="Czy załoga Apollo 11 widziała coś?")
        other = self._make_topic(author, title="Zwykły temat bez matcha")
        last_post = Post.objects.create(topic=matching, author=author, content_bbcode="Start", post_order=1)
        matching.last_post = last_post
        matching.last_post_at = last_post.created_at
        matching.save(update_fields=["last_post", "last_post_at"])
        Poll.objects.create(
            topic=matching,
            question="Pytanie?",
            is_closed=True,
            is_archived_import=True,
            total_votes=0,
        )
        Post.objects.create(topic=other, author=author, content_bbcode="Start 2", post_order=1)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "topics", "q": "apollo 11"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Czy załoga Apollo 11 widziała coś?")
        self.assertNotContains(response, "Zwykły temat bez matcha")
        self.assertContains(response, "ankieta")

    def test_search_topics_mode_can_filter_only_poll_topics_without_query(self):
        reader = User.objects.create_user(username="CzytelnikSearch2", password="x")
        author = User.objects.create_user(username="AutorSearch2", password="x")
        with_poll = self._make_topic(author, title="Temat z ankietą")
        without_poll = self._make_topic(author, title="Temat bez ankiety")
        with_poll_last = Post.objects.create(topic=with_poll, author=author, content_bbcode="Start", post_order=1)
        without_poll_last = Post.objects.create(topic=without_poll, author=author, content_bbcode="Start", post_order=1)
        with_poll.last_post = with_poll_last
        with_poll.last_post_at = with_poll_last.created_at
        with_poll.save(update_fields=["last_post", "last_post_at"])
        without_poll.last_post = without_poll_last
        without_poll.last_post_at = without_poll_last.created_at
        without_poll.save(update_fields=["last_post", "last_post_at"])
        Poll.objects.create(
            topic=with_poll,
            question="Pytanie?",
            is_closed=True,
            is_archived_import=True,
            total_votes=0,
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "topics", "kind": "polls"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Temat z ankietą")
        self.assertNotContains(response, "Temat bez ankiety")

    def test_search_posts_mode_can_filter_only_posts_with_links(self):
        reader = User.objects.create_user(username="CzytelnikSearch3", password="x")
        author = User.objects.create_user(username="AutorSearch3", password="x")
        topic = self._make_topic(author, title="Linki")
        Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='Zobacz [url=https://example.com]example[/url]',
            post_order=1,
        )
        Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Sam tekst bez linku",
            post_order=2,
        )
        rebuild_post_search_index_for_posts(
            Post.objects.filter(topic=topic).select_related("topic", "topic__forum", "author")
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "posts", "kind": "links"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "example")
        self.assertNotContains(response, "Sam tekst bez linku")

    def test_search_posts_mode_can_filter_only_posts_with_youtube(self):
        reader = User.objects.create_user(username="CzytelnikSearch4", password="x")
        author = User.objects.create_user(username="AutorSearch4", password="x")
        topic = self._make_topic(author, title="YouTube")
        Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='[youtube=https://www.youtube.com/watch?v=OtR8UWwIDjg][/youtube]',
            post_order=1,
        )
        Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Sam tekst bez filmu",
            post_order=2,
        )
        rebuild_post_search_index_for_posts(
            Post.objects.filter(topic=topic).select_related("topic", "topic__forum", "author")
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "posts", "kind": "youtube"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "youtube")
        self.assertNotContains(response, "Sam tekst bez filmu")

    def test_search_posts_mode_can_filter_by_author_without_query(self):
        reader = User.objects.create_user(username="CzytelnikSearch5", password="x")
        author = User.objects.create_user(username="ŁukaszSearch5", password="x")
        other = User.objects.create_user(username="InnySearch5", password="x")
        topic = self._make_topic(author, title="Autor filter posts")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść autora", post_order=1)
        Post.objects.create(topic=topic, author=other, content_bbcode="Treść innego", post_order=2)
        rebuild_post_search_index_for_posts(
            Post.objects.filter(topic=topic).select_related("topic", "topic__forum", "author")
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "posts", "author": "lukaszsearch5"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Treść autora")
        self.assertNotContains(response, "Treść innego")

    def test_search_topics_mode_can_filter_by_author_without_query(self):
        reader = User.objects.create_user(username="CzytelnikSearch6", password="x")
        author = User.objects.create_user(username="ŁukaszSearch6", password="x")
        other = User.objects.create_user(username="InnySearch6", password="x")
        own_topic = self._make_topic(author, title="Temat autora")
        other_topic = self._make_topic(other, title="Temat innego")
        Post.objects.create(topic=own_topic, author=author, content_bbcode="Start", post_order=1)
        Post.objects.create(topic=other_topic, author=other, content_bbcode="Start", post_order=1)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"mode": "topics", "author": "lukaszsearch6"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Temat autora")
        self.assertNotContains(response, "Temat innego")

    def test_new_topic_form_limits_title_length_to_70(self):
        form = NewTopicForm(data={
            "title": "x" * (TOPIC_TITLE_MAX_LENGTH + 1),
            "content": "Treść",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)

    def test_validate_post_content_uses_character_limit(self):
        _, _, errors = validate_post_content("x" * 20001)
        self.assertTrue(errors)
        self.assertIn("20001 znaków", errors[0])

    def test_validate_pm_content_uses_character_limit(self):
        _, _, errors = validate_pm_content("x" * 20001)
        self.assertTrue(errors)
        self.assertIn("20001 znaków", errors[0])

    def test_validate_pm_content_allows_only_shrinking_large_existing_message(self):
        _, _, errors = validate_pm_content("x" * 50001, original_size=50000)
        self.assertTrue(errors)
        self.assertIn("Wiadomość miała 50000 znaków", errors[0])

        _, _, errors = validate_pm_content("x" * 49999, original_size=50000)
        self.assertFalse(errors)

    def test_parse_poll_results_text(self):
        parsed = parse_poll_results_text(
            "Pytanie?\n"
            "Tak | 25% | [ 8 ]\n"
            "Nie | 75% | [ 24 ]\n"
            "Wszystkich Głosów : 32"
        )

        self.assertEqual(parsed["question"], "Pytanie?")
        self.assertEqual(parsed["total_votes"], 32)
        self.assertEqual(parsed["options"][0]["option_text"], "Tak")
        self.assertEqual(parsed["options"][0]["vote_count"], 8)
        self.assertEqual(parsed["options"][1]["option_text"], "Nie")
        self.assertEqual(parsed["options"][1]["vote_count"], 24)

    def test_validate_poll_option_count_uses_soft_limit(self):
        allowed, errors = validate_poll_option_count(33)

        self.assertEqual(allowed, 32)
        self.assertTrue(errors)
        self.assertIn("obecny limit to 32", errors[0])

    def test_validate_poll_option_count_allows_large_existing_poll_to_shrink_only(self):
        allowed, errors = validate_poll_option_count(48, original_count=48)

        self.assertEqual(allowed, 48)
        self.assertFalse(errors)

        allowed, errors = validate_poll_option_count(49, original_count=48)
        self.assertEqual(allowed, 48)
        self.assertTrue(errors)
        self.assertIn("Ankieta miała 48 opcji", errors[0])

    def test_validate_poll_option_count_rejects_above_hard_limit(self):
        allowed, errors = validate_poll_option_count(65)

        self.assertEqual(allowed, 32)
        self.assertTrue(errors)
        self.assertIn("twardy limit to 64", errors[0])

    def test_bbcode_repair_wraps_bare_non_youtube_url(self):
        repaired, changes = repair_bbcode("Zobacz https://example.com/test oraz opis.")

        self.assertIn("[url=https://example.com/test]https://example.com/test[/url]", repaired)
        self.assertTrue(changes)

    def test_bbcode_repair_wraps_bare_youtube_url_as_embed_tag(self):
        repaired, changes = repair_bbcode(
            "http://www.youtube.com/watch?v=OtR8UWwIDjg&feature=related"
        )

        self.assertIn(
            "[youtube=http://www.youtube.com/watch?v=OtR8UWwIDjg&feature=related][/youtube]",
            repaired,
        )
        self.assertTrue(changes)

    def test_archived_poll_models_store_results(self):
        author = User.objects.create_user(username="Autor9", password="x")
        topic = self._make_topic(author, title="Ankieta")
        poll = Poll.objects.create(
            topic=topic,
            question="Pytanie?",
            is_closed=True,
            is_archived_import=True,
            total_votes=32,
            imported_results_text="raw",
        )
        PollOption.objects.create(poll=poll, option_text="Tak", vote_count=8, sort_order=1)
        PollOption.objects.create(poll=poll, option_text="Nie", vote_count=24, sort_order=2)

        self.assertEqual(topic.poll.question, "Pytanie?")
        self.assertEqual(topic.poll.options.count(), 2)
        self.assertEqual(topic.poll.options.order_by("sort_order").first().option_text, "Tak")

    def test_topic_detail_renders_archived_poll_results(self):
        author = User.objects.create_user(username="Autor10", password="x")
        reader = User.objects.create_user(username="Czytelnik10", password="x")
        topic = self._make_topic(author, title="Ankieta render")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść posta", post_order=1)
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=True,
            is_archived_import=True,
            total_votes=10,
        )
        PollOption.objects.create(poll=poll, option_text="Tak", vote_count=7, sort_order=1)
        PollOption.objects.create(poll=poll, option_text="Nie", vote_count=3, sort_order=2)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ankieta")
        self.assertContains(response, "Czy tak?")
        self.assertContains(response, "Tak")
        self.assertContains(response, "Nie")
        self.assertContains(response, "Łącznie głosów: 10")

    def test_topic_detail_hides_nonzero_poll_results_before_vote(self):
        author = User.objects.create_user(username="AutorPollHide", password="x")
        reader = User.objects.create_user(username="CzytelnikPollHide", password="x")
        topic = self._make_topic(author, title="Ukryta ankieta")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść posta", post_order=1)
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=False,
            is_archived_import=False,
            total_votes=1,
        )
        yes = PollOption.objects.create(poll=poll, option_text="Tak", vote_count=1, sort_order=1)
        PollOption.objects.create(poll=poll, option_text="Nie", vote_count=0, sort_order=2)
        PollVote.objects.create(poll=poll, user=author, option=yes)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wyniki będą widoczne po oddaniu głosu")
        self.assertContains(response, "Głosuj")
        self.assertNotContains(response, "Łącznie głosów: 1")

    def test_vote_poll_records_vote_and_reveals_results(self):
        author = User.objects.create_user(username="AutorPollVote", password="x")
        voter = User.objects.create_user(username="CzytelnikPollVote", password="x")
        topic = self._make_topic(author, title="Głosowanie")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść posta", post_order=1)
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=False,
            is_archived_import=False,
            total_votes=0,
        )
        yes = PollOption.objects.create(poll=poll, option_text="Tak", vote_count=0, sort_order=1)
        PollOption.objects.create(poll=poll, option_text="Nie", vote_count=0, sort_order=2)

        client = Client()
        client.force_login(voter)
        response = client.post(reverse("vote_poll", args=[topic.pk]), {
            "poll_option": [str(yes.pk)],
        })

        self.assertEqual(response.status_code, 302)
        poll.refresh_from_db()
        yes.refresh_from_db()
        self.assertEqual(poll.total_votes, 1)
        self.assertEqual(yes.vote_count, 1)
        self.assertTrue(PollVote.objects.filter(poll=poll, user=voter, option=yes).exists())

        response = client.get(reverse("topic_detail", args=[topic.pk]))
        self.assertContains(response, "Łącznie głosów: 1")
        self.assertNotContains(response, "Wyniki będą widoczne po oddaniu głosu")
        self.assertContains(response, "X Twój głos")

    def test_poll_question_cannot_change_after_first_vote(self):
        author = User.objects.create_user(username="AutorPollLock1", password="x")
        topic = self._make_topic(author, title="Blokada ankiety")
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=False,
            is_archived_import=False,
            total_votes=1,
        )
        option = PollOption.objects.create(poll=poll, option_text="Tak", vote_count=1, sort_order=1)
        PollVote.objects.create(poll=poll, user=author, option=option)

        poll.question = "Czy jednak nie?"
        with self.assertRaises(ValidationError):
            poll.save()

    def test_poll_option_cannot_change_after_first_vote(self):
        author = User.objects.create_user(username="AutorPollLock2", password="x")
        topic = self._make_topic(author, title="Blokada odpowiedzi")
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=False,
            is_archived_import=False,
            total_votes=1,
        )
        option = PollOption.objects.create(poll=poll, option_text="Tak", vote_count=1, sort_order=1)
        PollVote.objects.create(poll=poll, user=author, option=option)

        option.option_text = "Nie"
        with self.assertRaises(ValidationError):
            option.save()

    def test_poll_option_cannot_be_deleted_after_first_vote(self):
        author = User.objects.create_user(username="AutorPollLock3", password="x")
        topic = self._make_topic(author, title="Blokada kasowania")
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=False,
            is_archived_import=False,
            total_votes=1,
        )
        option = PollOption.objects.create(poll=poll, option_text="Tak", vote_count=1, sort_order=1)
        PollVote.objects.create(poll=poll, user=author, option=option)

        with self.assertRaises(ValidationError):
            option.delete()

    def test_topic_detail_scales_poll_bars_to_highest_option(self):
        author = User.objects.create_user(username="AutorPollScale", password="x")
        reader = User.objects.create_user(username="CzytelnikPollScale", password="x")
        topic = self._make_topic(author, title="Skala ankiety")
        Post.objects.create(topic=topic, author=author, content_bbcode="Treść posta", post_order=1)
        poll = Poll.objects.create(
            topic=topic,
            question="Czy tak?",
            is_closed=True,
            is_archived_import=False,
            total_votes=10,
        )
        PollOption.objects.create(poll=poll, option_text="Tak", vote_count=4, sort_order=1)
        PollOption.objects.create(poll=poll, option_text="Nie", vote_count=2, sort_order=2)
        PollOption.objects.create(poll=poll, option_text="Może", vote_count=2, sort_order=3)
        PollOption.objects.create(poll=poll, option_text="Trudno powiedzieć", vote_count=2, sort_order=4)

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("topic_detail", args=[topic.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "paski w skali do najwyższej odpowiedzi")
        self.assertContains(response, "width:100%;", html=False)
        self.assertContains(response, "width:50%;", html=False)

    def test_extract_exact_quote_fragment_for_plain_text(self):
        fragment = extract_exact_quote_fragment("abc def ghi", "def")
        self.assertEqual(fragment, "def")

    def test_extract_exact_quote_fragment_keeps_inline_tags(self):
        fragment = extract_exact_quote_fragment("[b]abc[/b] [i]def[/i]", "abc def")
        self.assertEqual(fragment, "[b]abc[/b] [i]def[/i]")

    def test_extract_exact_quote_fragment_falls_back_for_nested_quote(self):
        fragment = extract_exact_quote_fragment('[quote="A"]abc[/quote] def', "abc")
        self.assertIsNone(fragment)

    def test_extract_exact_quote_fragment_keeps_nested_quote_with_post_id(self):
        fragment = extract_exact_quote_fragment(
            '[quote="Michał" post_id=10]x[quote="Semele" post_id=20]abc[/quote]y[/quote]',
            "abc",
        )
        self.assertEqual(fragment, '[quote="Semele" post_id=20]abc[/quote]')

    def test_extract_exact_quote_fragment_trims_nested_quote_with_ellipsis(self):
        fragment = extract_exact_quote_fragment(
            '[quote="Michał" post_id=10]x[quote="Semele" post_id=20]abc def ghi[/quote]y[/quote]',
            "def",
        )
        self.assertEqual(fragment, '[quote="Semele" post_id=20](...)\ndef\n(...)[/quote]')

    def test_extract_author_search_text_skips_quote_like_blocks(self):
        text = extract_author_search_text(
            'Ala [b]ma[/b] kota [quote="X"]ukryj[/quote] '
            '[fquote="Y"]też ukryj[/fquote] '
            '[Bible="J 1:1"]ukryj[/Bible] '
            '[AI="bot"]ukryj[/AI] [code]x=1[/code] i psa'
        )
        self.assertEqual(text, "Ala ma kota i psa")

    def test_rebuild_post_search_index_creates_author_only_text(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='Początek [quote="X"]ukryj[/quote] koniec',
            post_order=1,
        )

        total = rebuild_post_search_index_for_posts(
            Post.objects.filter(pk=post.pk).select_related("topic", "topic__forum", "author")
        )

        self.assertEqual(total, 1)
        post.refresh_from_db()
        self.assertEqual(post.search_index.content_search_author, "Początek koniec")
        self.assertEqual(post.search_index.topic_id, topic.pk)
        self.assertEqual(post.search_index.forum_id, topic.forum_id)

    def test_new_topic_form_accepts_poll_data(self):
        form = NewTopicForm(data={
            "title": "Temat z ankietą",
            "content": "Treść",
            "poll_enabled": "1",
            "poll_question": "Czy tak?",
            "poll_duration_days": "14",
            "poll_allow_vote_change": "1",
            "poll_options": ["Tak", "Nie", ""],
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["poll_data"]["question"], "Czy tak?")
        self.assertEqual(form.cleaned_data["poll_data"]["duration_days"], 14)
        self.assertEqual(form.cleaned_data["poll_data"]["options"], ["Tak", "Nie"])
        self.assertTrue(form.cleaned_data["poll_data"]["allow_vote_change"])
        self.assertFalse(form.cleaned_data["poll_data"]["allow_multiple_choice"])

    def test_new_topic_view_creates_poll_with_options(self):
        author = User.objects.create_user(username="AutorPoll", password="x")
        client = Client()
        client.force_login(author)

        response = client.post(reverse("new_topic", args=[self.forum.pk]), {
            "title": "Temat z ankietą",
            "content": "Treść główna",
            "poll_enabled": "1",
            "poll_question": "Czy tak?",
            "poll_duration_days": "14",
            "poll_allow_vote_change": "1",
            "poll_allow_multiple_choice": "1",
            "poll_options": ["Tak", "Nie", ""],
        })

        self.assertEqual(response.status_code, 302)
        topic = Topic.objects.get(title="Temat z ankietą")
        self.assertTrue(hasattr(topic, "poll"))
        self.assertEqual(topic.poll.question, "Czy tak?")
        self.assertTrue(topic.poll.allow_vote_change)
        self.assertTrue(topic.poll.allow_multiple_choice)
        self.assertFalse(topic.poll.is_archived_import)
        self.assertEqual(
            list(topic.poll.options.values_list("option_text", flat=True)),
            ["Tak", "Nie"],
        )

    def test_parse_search_query_skips_stop_words_but_keeps_phrases(self):
        parsed = _parse_search_query('do "do rzeczy" byt ale')

        self.assertEqual(parsed["phrases"], ["do rzeczy"])
        self.assertEqual(parsed["terms"], ["byt"])
        self.assertEqual(parsed["skipped_terms"], ["do", "ale"])

    def test_search_view_returns_indexed_post_match(self):
        author = User.objects.create_user(username="Autor", password="x")
        reader = User.objects.create_user(username="Czytelnik", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="Byt i świadomość",
            post_order=1,
        )

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("search"), {"q": "byt", "forum_id": topic.forum_id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wyszukiwarka")
        self.assertContains(response, topic.title)
        self.assertContains(response, post.search_index.content_search_author)

    def test_goto_post_redirects_to_correct_page(self):
        author = User.objects.create_user(username="Autor", password="x")
        reader = User.objects.create_user(username="Czytelnik", password="x")
        topic = self._make_topic(author)
        target_post = None
        for order in range(1, 26):
            post = Post.objects.create(
                topic=topic,
                author=author,
                content_bbcode=f"Post {order}",
                post_order=order,
            )
            if order == 25:
                target_post = post

        client = Client()
        client.force_login(reader)
        response = client.get(reverse("goto_post", args=[target_post.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/topic/{topic.pk}/?page=2#post-{target_post.pk}")

    def test_build_search_snippet_prefers_phrase(self):
        snippet = _build_search_snippet(
            "Ala ma kota i potem wolna wola wraca do tematu.",
            ["wolna wola"],
            ["ala", "tematu"],
            {"ala": 100, "tematu": 50},
            width=30,
        )
        self.assertIn("wolna", snippet)
        self.assertIn("<mark", snippet)

    def test_build_search_snippet_prefers_rarest_term(self):
        snippet = _build_search_snippet(
            "slowo popularne jest na początku, ale unikatowe trafienie jest dużo dalej w tym poście",
            [],
            ["popularne", "unikatowe"],
            {"popularne": 5000, "unikatowe": 10},
            width=32,
        )
        self.assertIn("unikatowe", snippet)
        self.assertIn("background:#d96a00", snippet)

    def test_matches_search_text_requires_word_boundaries_for_phrase(self):
        self.assertFalse(_matches_search_text("do rzeczywistego", ["do rzeczy"], []))
        self.assertTrue(_matches_search_text("to jest do rzeczy i tyle", ["do rzeczy"], []))

    def test_matches_search_text_requires_word_boundaries_for_term(self):
        self.assertFalse(_matches_search_text("zalobe", [], ["obe"]))
        self.assertTrue(_matches_search_text("to jest zalobe", [], ["zalobe"]))

    def test_build_search_snippet_highlights_without_diacritics(self):
        snippet = _build_search_snippet(
            "To była żałobę po kimś ważnym.",
            [],
            ["zalobe"],
            {"zalobe": 10},
            width=40,
        )
        self.assertIn("żałobę", snippet)
        self.assertIn("background:#d96a00", snippet)

    def test_quote_fragment_endpoint_returns_exact_source_when_safe(self):
        author = User.objects.create_user(username="Autor", password="x")
        reader = User.objects.create_user(username="Czytelnik", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode="[b]abc[/b] [i]def[/i]",
            post_order=1,
        )

        client = Client()
        client.force_login(reader)
        response = client.post(
            reverse("quote_fragment", args=[post.pk]),
            {"selected_text": "abc def"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "ok": True,
                "body": "[b]abc[/b] [i]def[/i]",
                "exact_source": True,
            },
        )

    def test_validate_enriched_quotes_requires_post_id(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        Post.objects.create(topic=topic, author=author, content_bbcode="abc def", post_order=1)

        errors = validate_enriched_quotes('[quote="Autor"]abc[/quote]')

        self.assertEqual(errors, ["Cytat [quote] musi zawierać post_id."])

    def test_validate_enriched_quotes_requires_matching_author(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="abc def", post_order=1)

        errors = validate_enriched_quotes(f'[quote="Ktoś" post_id={post.pk}]abc def[/quote]')

        self.assertEqual(
            errors,
            [f'Cytat z post_id={post.pk} musi mieć autora "Autor", a ma "Ktoś".'],
        )

    def test_validate_enriched_quotes_rejects_fake_fragment(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="abc def ghi", post_order=1)

        errors = validate_enriched_quotes(f'[quote="Autor" post_id={post.pk}]xyz[/quote]')

        self.assertEqual(
            errors,
            [f"Cytowany fragment dla post_id={post.pk} nie pasuje do treści źródłowej."],
        )

    def test_validate_enriched_quotes_allows_ellipsis(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(topic=topic, author=author, content_bbcode="abc def ghi jkl mno", post_order=1)

        errors = validate_enriched_quotes(
            f'[quote="Autor" post_id={post.pk}]abc (...) jkl /.../ mno[/quote]'
        )

        self.assertEqual(errors, [])

    def test_quote_refs_store_ellipsis_count(self):
        author = User.objects.create_user(username="Autor", password="x")
        topic = self._make_topic(author)
        post = Post.objects.create(
            topic=topic,
            author=author,
            content_bbcode='[quote="Autor" post_id=123]abc (...) def /.../ ghi[/quote]',
            post_order=1,
        )

        rebuild_quote_references_for_post(post)
        ref = post.quote_references.get()

        self.assertEqual(ref.ellipsis_count, 2)
