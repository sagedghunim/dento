from unittest import mock

from django.test import SimpleTestCase, override_settings


@override_settings(ROOT_URLCONF="config.urls")
class DailyCronTests(SimpleTestCase):
    def test_off_without_secret(self):
        with mock.patch.dict("os.environ", {"CRON_SECRET": ""}):
            self.assertEqual(self.client.get("/cron/daily").status_code, 404)

    def test_wrong_secret_is_404(self):
        with mock.patch.dict("os.environ", {"CRON_SECRET": "s3cret"}):
            response = self.client.get("/cron/daily", HTTP_AUTHORIZATION="Bearer nope")
            self.assertEqual(response.status_code, 404)

    def test_runs_every_step_even_when_one_fails(self):
        with mock.patch.dict("os.environ", {"CRON_SECRET": "s3cret"}), \
             mock.patch("core.partitions.ensure_all", return_value={}), \
             mock.patch("scheduling.jobs.run_sweep", side_effect=RuntimeError("x")), \
             mock.patch("scheduling.jobs.run_recompute", return_value=[]) as recompute, \
             mock.patch("core.jobs.clean_request_keys", return_value=0) as keys:
            response = self.client.get("/cron/daily", HTTP_AUTHORIZATION="Bearer s3cret")
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertFalse(body["sweep"]["ok"])
        self.assertTrue(body["summaries"]["ok"])
        recompute.assert_called_once()
        keys.assert_called_once()
