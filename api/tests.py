import json
from unittest.mock import patch

from django.test import TestCase

from .cloudbase_nosql import decode_ejson


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
