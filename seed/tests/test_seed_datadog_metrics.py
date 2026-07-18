import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import requests

from seed import seed_datadog_metrics
from seed.seed_datadog_metrics import (
    DATADOG_SITES,
    generate_metric_batches,
    validate_datadog_site,
)


class DatadogSiteValidationTests(unittest.TestCase):
    def test_accepts_each_supported_site_case_insensitively(self) -> None:
        for site in DATADOG_SITES:
            with self.subTest(site=site):
                self.assertEqual(validate_datadog_site(f" {site.upper()} "), site)

    def test_rejects_urls_paths_ports_and_unknown_hosts(self) -> None:
        invalid = (
            "https://datadoghq.com",
            "datadoghq.com/api/v2/series",
            "datadoghq.com:443",
            "datadoghq.com@example.invalid",
            "example.invalid",
            "",
        )
        for site in invalid:
            with self.subTest(site=site):
                with self.assertRaises(ValueError):
                    validate_datadog_site(site)


class MetricGenerationTests(unittest.TestCase):
    NOW = 2_000_000_000.0

    def test_same_seed_and_anchor_produce_same_dataset(self) -> None:
        first = generate_metric_batches(self.NOW, 42)
        second = generate_metric_batches(self.NOW, 42)
        self.assertEqual(first, second)

    def test_different_seeds_change_values_but_not_shape(self) -> None:
        first = generate_metric_batches(self.NOW, 42)
        second = generate_metric_batches(self.NOW, 43)
        self.assertNotEqual(first, second)
        self.assertEqual([label for label, _ in first], [label for label, _ in second])

    def test_every_series_covers_four_hours_at_one_minute_resolution(self) -> None:
        batches = generate_metric_batches(self.NOW, 42)
        self.assertEqual(len(batches), 6)
        for label, series in batches:
            with self.subTest(label=label):
                self.assertEqual(len(series), 1)
                points = series[0]["points"]
                self.assertEqual(len(points), 241)
                self.assertEqual(points[0]["timestamp"], int(self.NOW - 4 * 3600))
                self.assertEqual(points[-1]["timestamp"], int(self.NOW))

    def test_incident_signals_are_present(self) -> None:
        batches = dict(generate_metric_batches(self.NOW, 42))
        export = batches["export endpoint latency (/api/v1/items/export)"][0]["points"]
        healthy = batches["items endpoint latency (/api/v1/items — healthy)"][0][
            "points"
        ]
        errors = batches["error count (timeouts)"][0]["points"]

        self.assertLess(max(point["value"] for point in export[:120]), 300)
        self.assertGreater(max(point["value"] for point in export[-60:]), 5_000)
        self.assertLess(max(point["value"] for point in healthy), 125)
        self.assertEqual({point["value"] for point in errors[:180]}, {0.0})
        self.assertGreater(min(point["value"] for point in errors[180:]), 0)


class DatadogApplyFailureTests(unittest.TestCase):
    @mock.patch.object(seed_datadog_metrics.requests, "post")
    def test_unexpected_success_status_is_still_a_failure(
        self, post: mock.Mock
    ) -> None:
        response = post.return_value
        response.status_code = 204
        response.text = ""

        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(requests.HTTPError, "unexpected Datadog"),
        ):
            seed_datadog_metrics.submit_series(
                [{"metric": "agent.test", "type": 0, "points": []}],
                "test-api-key",
                "test-app-key",
                "datadoghq.com",
            )

    @mock.patch.object(
        seed_datadog_metrics,
        "generate_metric_batches",
        return_value=[
            (
                "failing metric",
                [
                    {
                        "metric": "agent.test",
                        "type": 0,
                        "points": [{"timestamp": 1, "value": 1}],
                    }
                ],
            )
        ],
    )
    @mock.patch.object(
        seed_datadog_metrics,
        "submit_series",
        side_effect=requests.ConnectionError("provider unavailable"),
    )
    def test_apply_returns_nonzero_without_false_success(
        self,
        _submit: mock.Mock,
        _generate: mock.Mock,
    ) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"DD_API_KEY": "test-api-key", "DD_APP_KEY": "test-app-key"},
                clear=False,
            ),
            mock.patch.object(sys, "argv", ["seed_datadog_metrics.py", "--apply"]),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = seed_datadog_metrics.main()

        self.assertEqual(exit_code, 1)
        self.assertNotIn("Done.", stdout.getvalue())
        self.assertIn("Datadog seed failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
