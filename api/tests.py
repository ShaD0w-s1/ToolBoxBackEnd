import json
from unittest.mock import patch

from django.test import TestCase

from .cloudbase_nosql import decode_ejson
from .polling import PollingPayloadError, calculate_revision


class FakeNoSQLClient:
    def __init__(self):
        self.inserted = []

    def insert_document(self, collection, document):
        self.inserted.append((collection, document))
        return {"insertedIds": [document["_id"]]}

    def list_documents(self, collection, **kwargs):
        return {"offset": 0, "limit": kwargs["limit"], "list": []}


class ApiSmokeTests(TestCase):
    def test_index_returns_json(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Django API is running")

    def test_local_vue_origin_is_allowed(self):
        response = self.client.get(
            "/",
            HTTP_ORIGIN="http://localhost:5173",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "http://localhost:5173",
        )

    def test_cloudbase_status_does_not_expose_secret(self):
        response = self.client.get("/api/cloudbase/status/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("api_key", response.json())

    def test_csrf_returns_a_token_for_cross_origin_clients(self):
        response = self.client.get("/api/csrf/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["csrf_token"])

    def test_create_project_maps_business_document(self):
        fake = FakeNoSQLClient()
        payload = {
            "name": "A320 定检",
            "aircraft_type": "A320",
            "team": "A1",
            "sections": [
                {
                    "name": "A",
                    "note": "前舱",
                    "tasks": [
                        {
                            "name": "工作 A1",
                            "items": [{"name": "扳手", "quantity": 2}],
                        }
                    ],
                }
            ],
        }

        with patch("api.views.get_nosql_client", return_value=fake):
            response = self.client.post(
                "/api/projects/",
                data=json.dumps(payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 201)
        document = fake.inserted[0][1]
        self.assertEqual(document["team"], "A1")
        self.assertEqual(document["sections"][0]["tasks"][0]["items"][0]["quantity"], 2)
        self.assertEqual(document["version"], 1)

    def test_project_list_exposes_stable_data_field(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {
            "offset": 0,
            "limit": kwargs["limit"],
            "list": [{"_id": "project-1", "name": "Persisted"}],
        }

        with patch("api.views.get_nosql_client", return_value=fake):
            response = self.client.get("/api/projects/?limit=100")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["_id"], "project-1")


class NinjaApiTests(TestCase):
    def test_swagger_docs_page_renders(self):
        response = self.client.get("/api/docs")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "swagger-ui")

    def test_openapi_schema_describes_typed_examples(self):
        response = self.client.get("/api/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/api/hello", paths)
        self.assertIn("/api/project-preview", paths)

    def test_project_preview_validates_and_returns_payload(self):
        response = self.client.post(
            "/api/project-preview",
            data=json.dumps(
                {
                    "name": "A320 Check",
                    "aircraft_type": "A320",
                    "team": "A1",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["aircraft_type"], "A320")

    def test_project_preview_rejects_unknown_aircraft_type(self):
        response = self.client.post(
            "/api/project-preview",
            data=json.dumps({"name": "Demo", "aircraft_type": "C919"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 422)


class EJsonTests(TestCase):
    def test_decodes_cloudbase_strict_ejson(self):
        value = {
            "_id": {"$oid": "abc"},
            "quantity": {"$numberInt": "2"},
            "nested": [{"version": {"$numberLong": "3"}}],
        }

        self.assertEqual(
            decode_ejson(value),
            {"_id": "abc", "quantity": 2, "nested": [{"version": 3}]},
        )


class PollingTests(TestCase):
    def test_unknown_cloudbase_payload_is_rejected(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {"unexpected": []}

        with self.assertRaises(PollingPayloadError):
            calculate_revision(fake, ["projects"])

    def test_non_document_list_item_is_rejected(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {
            "data": [{"_id": "valid"}, "invalid"]
        }

        with self.assertRaisesRegex(PollingPayloadError, "第 1 项不是对象"):
            calculate_revision(fake, ["projects"])

    def test_poll_returns_bad_gateway_for_unknown_payload(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {"unexpected": []}

        with patch("api.views.get_nosql_client", return_value=fake):
            response = self.client.get("/api/poll/")

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["ok"])

    def test_revision_is_stable_when_document_order_changes(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {
            "data": [
                {"_id": "b", "version": 2},
                {"_id": "a", "version": 1},
            ]
        }
        first = calculate_revision(fake, ["projects"])
        fake.list_documents = lambda collection, **kwargs: {
            "data": [
                {"_id": "a", "version": 1},
                {"_id": "b", "version": 2},
            ]
        }

        self.assertEqual(calculate_revision(fake, ["projects"]), first)

    def test_poll_reports_a_changed_revision(self):
        fake = FakeNoSQLClient()
        fake.list_documents = lambda collection, **kwargs: {
            "data": [{"_id": collection, "version": 1}]
        }
        with patch("api.views.get_nosql_client", return_value=fake):
            baseline = self.client.get("/api/poll/").json()["revision"]
            fake.list_documents = lambda collection, **kwargs: {
                "data": [{"_id": collection, "version": 2}]
            }
            response = self.client.get(f"/api/poll/?revision={baseline}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
