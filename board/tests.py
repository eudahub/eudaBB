from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .models import Forum, Section, Topic, User, Post
from .quote_refs import rebuild_quote_references_for_post, rebuild_quote_references_for_posts
from .user_rename import rename_user_and_update_quotes
from .username_utils import normalize


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
