import json
import unittest

from fantasy_analytics.client import (
    ClientConfig,
    GraphQLRequestError,
    SportsGraphQLClient,
)


class SportsGraphQLClientTest(unittest.TestCase):
    def test_execute_sends_json_and_returns_payload(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            captured["content_type"] = request.headers["Content-type"]
            return json.dumps({"data": {"ok": True}}).encode()

        client = SportsGraphQLClient(
            ClientConfig(timeout_seconds=12, attempts=1),
            transport=transport,
        )

        result = client.execute("query Test($id: ID!) { node(id: $id) }", {"id": "1"})

        self.assertEqual({"data": {"ok": True}}, result)
        self.assertEqual({"id": "1"}, captured["body"]["variables"])
        self.assertEqual(12, captured["timeout"])
        self.assertEqual("application/json", captured["content_type"])

    def test_execute_rejects_graphql_errors(self) -> None:
        def transport(_request, _timeout):
            return json.dumps({"errors": [{"message": "broken"}]}).encode()

        client = SportsGraphQLClient(
            ClientConfig(attempts=1),
            transport=transport,
        )

        with self.assertRaises(GraphQLRequestError):
            client.execute("query Broken { broken }")

    def test_execute_rejects_missing_data(self) -> None:
        def transport(_request, _timeout):
            return b"{}"

        client = SportsGraphQLClient(
            ClientConfig(attempts=1),
            transport=transport,
        )

        with self.assertRaises(GraphQLRequestError):
            client.execute("query Empty { empty }")


if __name__ == "__main__":
    unittest.main()
