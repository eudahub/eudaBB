from django.core.exceptions import ValidationError
from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse

from .models import Forum, Section, Topic, User, Post
from .quote_refs import rebuild_quote_references_for_post, rebuild_quote_references_for_posts
from .quote_selection import extract_exact_quote_fragment
from .quote_validation import validate_enriched_quotes
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
        self.assertContains(response, "Regulamin")
        self.assertContains(response, "Ogłoszenia")
        self.assertContains(response, "Treść przypięta")
        self.assertContains(response, f'data-post-id="{pinned_post.pk}"', html=False)

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
