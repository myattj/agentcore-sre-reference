import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from seed import seed_slack_threads
from seed.seed_slack_threads import (
    SlackApiError,
    _post_message,
    _thread_url,
    build_thread_specs,
    validate_seeder_token,
    validate_slack_channel_id,
    validate_slack_user_id,
)


class SlackInputValidationTests(unittest.TestCase):
    def test_accepts_normal_slack_ids(self) -> None:
        self.assertEqual(validate_slack_channel_id(" c0123456789 "), "C0123456789")
        self.assertEqual(validate_slack_channel_id("G0123456789"), "G0123456789")
        self.assertEqual(validate_slack_user_id(" u0123456789 "), "U0123456789")

    def test_rejects_names_urls_and_malformed_ids(self) -> None:
        for channel in ("general", "#general", "https://slack.com/general", "C123", ""):
            with self.subTest(channel=channel):
                with self.assertRaises(ValueError):
                    validate_slack_channel_id(channel)
        for user_id in ("morgan", "@morgan", "C0123456789", "U123", ""):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    validate_slack_user_id(user_id)

    def test_requires_a_bot_token_without_whitespace(self) -> None:
        self.assertEqual(validate_seeder_token("xoxb-test-token"), "xoxb-test-token")
        for token in ("xoxp-user-token", "Bearer xoxb-token", "xoxb-token\n", ""):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    validate_seeder_token(token)


class SlackStoryTests(unittest.TestCase):
    def test_builds_three_complete_threads_without_real_org_names(self) -> None:
        specs = build_thread_specs(None)
        self.assertEqual(len(specs), 3)
        self.assertEqual([len(spec.replies) for spec in specs], [4, 3, 2])
        story = "\n".join(
            [text for spec in specs for text in (spec.root, *spec.replies)]
        )
        self.assertIn("acme-labs/data-api", story)

    def test_optional_agent_user_is_rendered_as_a_real_mention(self) -> None:
        specs = build_thread_specs("U0123456789")
        replies = "\n".join(reply for spec in specs for reply in spec.replies)
        self.assertIn("<@U0123456789>", replies)

    def test_thread_url_is_deterministic(self) -> None:
        self.assertEqual(
            _thread_url("C0123456789", "1712345678.123456"),
            "https://slack.com/archives/C0123456789/p1712345678123456",
        )


class SlackApplyFailureTests(unittest.TestCase):
    @mock.patch.object(seed_slack_threads.requests, "post")
    def test_api_rejection_raises(self, post: mock.Mock) -> None:
        response = post.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": False, "error": "not_in_channel"}

        with self.assertRaisesRegex(SlackApiError, "not_in_channel"):
            _post_message("xoxb-test-token", "C0123456789", "hello")

    @mock.patch.object(
        seed_slack_threads,
        "post_thread",
        side_effect=SlackApiError("provider unavailable"),
    )
    def test_apply_returns_nonzero_without_false_success(
        self,
        _post_thread: mock.Mock,
    ) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SLACK_SEEDER_BOT_TOKEN": "xoxb-test-token",
                    "SLACK_SEED_CHANNEL_ID": "C0123456789",
                },
                clear=False,
            ),
            mock.patch.object(sys, "argv", ["seed_slack_threads.py", "--apply"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = seed_slack_threads.main()

        self.assertEqual(exit_code, 1)
        self.assertNotIn("Done.", stdout.getvalue())
        self.assertIn("Slack seed failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
