import re

import requests
import urllib3

from Resources.config import Config
from Resources.logger_config import get_logger

logger = get_logger("XrayAuto")


class XrayClient:
    """
    Facade that keeps the public Xray client API stable while selecting the
    deployment-specific implementation.
    """
    def __init__(self, deployment=None, **kwargs):
        self.deployment = (deployment or Config.XRAY_DEPLOYMENT or "datacenter").strip().lower()

        if self.deployment == "datacenter":
            self._client = XrayDataCenterClient(**kwargs)
        elif self.deployment == "cloud":
            self._client = XrayCloudClient(**kwargs)
        else:
            raise ValueError("XRAY_DEPLOYMENT must be either 'datacenter' or 'cloud'")

        logger.info("Using Xray deployment: %s", self.deployment)

    def get_test_summary(self, test_key):
        return self._client.get_test_summary(test_key)

    def get_test_steps(self, test_key):
        return self._client.get_test_steps(test_key)

    def get_test_execution_tests(self, execution_key):
        return self._client.get_test_execution_tests(execution_key)


class XrayDataCenterClient:
    """
    A client class to interact with the Jira/Xray Data Center API.
    Handles authentication, making requests, and error handling.
    """
    def __init__(self, base_url=None, token=None, verify_ssl=None, timeout=30):
        self.base_url = (base_url or Config.JIRA_DATACENTER_URL or "").rstrip("/")
        self.token = token or Config.JIRA_DATACENTER_API_TOKEN
        self.verify_ssl = Config.JIRA_DATACENTER_VERIFY_SSL if verify_ssl is None else verify_ssl
        self.timeout = timeout

        if not self.base_url:
            raise ValueError("JIRA_DATACENTER_URL must be configured before using XrayDataCenterClient")

        if not self.token:
            raise ValueError("JIRA_DATACENTER_API_TOKEN must be configured before using XrayDataCenterClient")

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _headers(self):
        """
        Helper method to construct the HTTP headers required for Jira/Xray APIs.
        """
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _get_json(self, path, params=None):
        """
        Core private method to execute a GET request and return the JSON response.
        Handles URL construction, making the request, and basic error checking.
        """
        url = f"{self.base_url}{path}"
        
        response = requests.get(
            url,
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )

        logger.debug(f"Response: {response.text}")


        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            message = response.text.strip() or "Unexpected non-JSON response"
            raise RuntimeError(
                f"Data Center request failed for {path} ({response.status_code}): {message}"
            )

        # Parse the JSON payload from the response. Some Jira/Xray endpoints can
        # return plain-text error bodies even when the content type says JSON.
        try:
            payload = response.json()
        except ValueError as exc:
            message = response.text.strip() or "Empty or invalid JSON response"
            raise RuntimeError(
                f"Data Center request failed for {path} ({response.status_code}): {message}"
            ) from exc
        
        if not response.ok:
            raise RuntimeError(f"Data Center request failed for {path} ({response.status_code}): {payload}")

        return payload

    def get_test_summary(self, test_key):
        """
        Fetches the basic details (summary, description) of a Jira issue.
        """
        return self._get_json(
            f"/rest/api/2/issue/{test_key}",
            params={"fields": "summary,description"},
        )

    def get_test_steps(self, test_key):
        """
        Fetches the manual test steps defined inside an Xray Test issue.
        Uses the Raven 2.0 API endpoint.
        """
        return self._get_json(f"/rest/raven/2.0/api/test/{test_key}/steps")

    def get_test_execution_tests(self, execution_key):
        """
        Fetches the list of tests associated with a specific Xray Test Execution.
        Uses the Raven 1.0 API endpoint.
        Returns the raw JSON response, which may contain a list of tests under a specific key (e.g., "tests").
        """
        return self._get_json(f"/rest/raven/1.0/api/testexec/{execution_key}/test")


class XrayCloudClient:
    """
    A client class to interact with Jira Cloud and Xray Cloud GraphQL APIs.

    Xray Cloud needs both Jira Cloud credentials and Xray Cloud API credentials:
    - Jira Cloud is used to resolve issue keys such as PROJ-123 to Jira issue ids.
    - Xray Cloud GraphQL is used to read Xray-specific data such as manual steps.
    """
    def __init__(
        self,
        jira_base_url=None,
        jira_email=None,
        jira_token=None,
        xray_base_url=None,
        client_id=None,
        client_secret=None,
        verify_ssl=True,
        timeout=30,
    ):
        self.jira_base_url = (jira_base_url or Config.JIRA_CLOUD_URL or "").rstrip("/")
        self.jira_email = jira_email or Config.JIRA_CLOUD_EMAIL
        self.jira_token = jira_token or Config.JIRA_CLOUD_API_TOKEN
        self.xray_base_url = (xray_base_url or Config.XRAY_CLOUD_API_URL or "").rstrip("/")
        self.client_id = client_id or Config.XRAY_CLOUD_CLIENT_ID
        self.client_secret = client_secret or Config.XRAY_CLOUD_CLIENT_SECRET
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._xray_token = None

        if not self.jira_base_url:
            raise ValueError("JIRA_CLOUD_URL must be configured before using XrayCloudClient")
        if not self.jira_email:
            raise ValueError("JIRA_CLOUD_EMAIL must be configured before using XrayCloudClient")
        if not self.jira_token:
            raise ValueError("JIRA_CLOUD_API_TOKEN must be configured before using XrayCloudClient")
        if not self.xray_base_url:
            raise ValueError("XRAY_CLOUD_API_URL must be configured before using XrayCloudClient")
        if not self.client_id:
            raise ValueError("XRAY_CLOUD_CLIENT_ID must be configured before using XrayCloudClient")
        if not self.client_secret:
            raise ValueError("XRAY_CLOUD_CLIENT_SECRET must be configured before using XrayCloudClient")

    def _jira_get_json(self, path, params=None):
        """
        Run a Jira Cloud REST GET request with basic auth.

        Example:
            path="/rest/api/3/issue/PROJ-123"
            params={"fields": "summary"}

        Example output:
            {"id": "10042", "key": "PROJ-123", "fields": {"summary": "Login test"}}
        """
        url = f"{self.jira_base_url}{path}"
        response = requests.get(
            url,
            auth=(self.jira_email, self.jira_token),
            headers={"Accept": "application/json"},
            params=params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        return self._parse_json_response(response, f"Jira Cloud request failed for {path}")

    def _authenticate_xray(self):
        """
        Exchange Xray Cloud client_id/client_secret for a short-lived API token.

        Example request body:
            {"client_id": "...", "client_secret": "..."}

        Example output:
            "eyJhbGciOi..."
        """
        if self._xray_token:
            return self._xray_token

        path = "/api/v2/authenticate"
        url = f"{self.xray_base_url}{path}"
        response = requests.post(
            url,
            json={"client_id": self.client_id, "client_secret": self.client_secret},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        payload = self._parse_json_response(response, f"Xray Cloud authentication failed for {path}")
        if isinstance(payload, str):
            token = payload.strip().strip('"')
        elif isinstance(payload, dict):
            token = str(payload.get("token") or payload.get("access_token") or "").strip()
        else:
            token = ""

        if not token:
            raise RuntimeError("Xray Cloud authentication response did not contain a token")

        self._xray_token = token
        return token

    def _graphql(self, query, variables=None):
        """
        Run an authenticated Xray Cloud GraphQL request.

        Example variables:
            {"issueId": "10042"}

        Example output:
            {"getTest": {"issueId": "10042", "steps": [...]}}
        """
        path = "/api/v2/graphql"
        url = f"{self.xray_base_url}{path}"
        response = requests.post(
            url,
            json={"query": query, "variables": variables or {}},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._authenticate_xray()}",
            },
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        payload = self._parse_json_response(response, f"Xray Cloud GraphQL request failed for {path}")
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(f"Xray Cloud GraphQL returned errors: {errors}")
        return payload.get("data", {}) if isinstance(payload, dict) else {}

    @staticmethod
    def _parse_json_response(response, error_prefix):
        """
        Parse JSON responses and raise consistent RuntimeError messages.

        Xray Cloud authentication can return a JSON string token, while Jira and
        GraphQL normally return JSON objects. This helper accepts both shapes.

        It does not extract fields such as summary by itself; it returns the
        parsed response body as-is.

        Example Jira issue output:
            {
              "id": "10042",
              "key": "PROJ-123",
              "fields": {"summary": "Login test"}
            }

        Example Xray auth output:
            "eyJhbGciOi..."

        Example GraphQL output:
            {
              "data": {
                "getTest": {
                  "issueId": "10042",
                  "steps": [{"action": "Click login", "result": "Dashboard opens"}]
                }
              }
            }
        """
        content_type = response.headers.get("Content-Type", "")
        text = response.text.strip()

        if "application/json" not in content_type:
            if response.ok and text:
                return text.strip('"')
            message = text or "Unexpected non-JSON response"
            raise RuntimeError(f"{error_prefix} ({response.status_code}): {message}")

        try:
            payload = response.json()
        except ValueError as exc:
            message = text or "Empty or invalid JSON response"
            raise RuntimeError(f"{error_prefix} ({response.status_code}): {message}") from exc

        if not response.ok:
            raise RuntimeError(f"{error_prefix} ({response.status_code}): {payload}")
        return payload

    def _get_jira_issue(self, issue_key_or_id, fields):
        """
        Fetch a Jira Cloud issue by issue key or numeric issue id.

        Example:
            issue_key_or_id="PROJ-123"
            fields=["summary", "description"]

        Example output:
            {
              "id": "10042",
              "key": "PROJ-123",
              "fields": {"summary": "Login test", "description": "..."}
            }
        """
        return self._jira_get_json(
            f"/rest/api/3/issue/{issue_key_or_id}",
            params={"fields": ",".join(fields)},
        )

    @staticmethod
    def _step_field(raw_value):
        """
        Wrap one Cloud step cell value in the shape expected by xray_normalizer.

        Example:
            "Click login" -> {"value": {"raw": "Click login"}}
        """
        return {"value": {"raw": "" if raw_value is None else str(raw_value)}}

    @staticmethod
    def _clean_cloud_text(value):
        """
        Convert Jira/Xray Cloud wiki-style links to plain text before execution.

        Xray Cloud may return links as [label|target]. For automation steps, the
        target is the executable value we need.

        Examples:
            "[https://www.wikipedia.org|https://www.wikipedia.org/]"
                -> "https://www.wikipedia.org/"
            "Navigate to \"[Home|https://example.com]\""
                -> "Navigate to \"https://example.com\""
        """
        text = "" if value is None else str(value)
        cleaned = re.sub(r"\[([^\]|]+)\|([^\]]+)\]", r"\2", text)
        return re.sub(r"(https?://[^/\s\"']+)//(?=($|[\s\"']))", r"\1/", cleaned)

    @classmethod
    def _normalize_steps(cls, steps):
        """
        Convert Xray Cloud GraphQL steps to the existing Data Center-like payload.

        Example input:
            [{"action": "Navigate to \"[Home|https://example.com]\"", "result": "Dashboard opens"}]

        Example output:
            {
              "steps": [
                {
                  "fields": {
                    "Action": {"value": {"raw": "Navigate to \"https://example.com\""}},
                    "Expected Result": {"value": {"raw": "Dashboard opens"}}
                  }
                }
              ]
            }
        """
        normalized_steps = []
        for step in steps or []:
            normalized_steps.append(
                {
                    "fields": {
                        "Action": cls._step_field(cls._clean_cloud_text(step.get("action", ""))),
                        "Expected Result": cls._step_field(cls._clean_cloud_text(step.get("result", ""))),
                    }
                }
            )
        return {"steps": normalized_steps}

    @staticmethod
    def _extract_jira_key_from_graphql(test):
        """
        Read the Jira issue key embedded in an Xray GraphQL test result.

        Example input:
            {"issueId": "10042", "jira": {"key": "PROJ-123"}}

        Example output:
            "PROJ-123"
        """
        jira_payload = test.get("jira") if isinstance(test, dict) else None
        if isinstance(jira_payload, dict):
            key = jira_payload.get("key")
            if key:
                return str(key).strip()
        return ""

    def get_test_summary(self, test_key):
        """
        Return the Jira issue payload used by format_xray_scenario.

        Example output:
            {"id": "10042", "key": "PROJ-123", "fields": {"summary": "Login test"}}
        """
        return self._get_jira_issue(test_key, ["summary", "description"])

    def get_test_steps(self, test_key):
        """
        Return manual Xray steps for a Cloud Test issue in normalizer format.

        Flow:
            PROJ-123 -> Jira issue id 10042 -> getTest(issueId: "10042")

        Example output:
            {"steps": [{"fields": {"Action": {"value": {"raw": "Click login"}}}}]}
        """
        issue = self._get_jira_issue(test_key, ["summary"])
        issue_id = str(issue.get("id") or "").strip()
        if not issue_id:
            raise RuntimeError(f"Jira Cloud issue {test_key} did not include an id")

        query = """
        query GetTest($issueId: String!) {
          getTest(issueId: $issueId) {
            issueId
            steps {
              action
              result
            }
          }
        }
        """
        data = self._graphql(query, {"issueId": issue_id})
        test = data.get("getTest") or {}
        return self._normalize_steps(test.get("steps") or [])

    def get_test_execution_tests(self, execution_key):
        """
        Return tests included in a Cloud Test Execution.

        Flow:
            PROJ-999 -> Jira issue id 10099 -> getTestExecution(issueId: "10099")

        Example output:
            {"tests": [{"key": "PROJ-123"}, {"key": "PROJ-124"}]}
        """
        issue = self._get_jira_issue(execution_key, ["summary"])
        issue_id = str(issue.get("id") or "").strip()
        if not issue_id:
            raise RuntimeError(f"Jira Cloud issue {execution_key} did not include an id")

        query = """
        query GetTestExecution($issueId: String!, $start: Int!, $limit: Int!) {
          getTestExecution(issueId: $issueId) {
            tests(start: $start, limit: $limit) {
              total
              start
              limit
              results {
                issueId
                jira(fields: ["key"])
              }
            }
          }
        }
        """
        limit = 100
        start = 0
        items = []

        while True:
            data = self._graphql(query, {"issueId": issue_id, "start": start, "limit": limit})
            execution = data.get("getTestExecution") or {}
            tests = execution.get("tests") or {}
            results = tests.get("results") or []

            for test in results:
                key = self._extract_jira_key_from_graphql(test)
                if not key and test.get("issueId"):
                    key = self._get_jira_issue(str(test["issueId"]), ["summary"]).get("key", "")
                if key:
                    items.append({"key": key})

            total = int(tests.get("total") or len(items))
            start += limit
            if start >= total or not results:
                break

        return {"tests": items}
