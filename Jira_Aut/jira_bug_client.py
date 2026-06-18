import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

from Resources.config import Config
from Resources.logger_config import get_logger


logger = get_logger("JiraBugClient")


@dataclass
class FailureReport:
    xray_test_key: str
    status: str
    summary: str
    failed_step_no: int | None = None
    failed_step_text: str = ""
    expected_result: str = ""
    actual_result: str = ""
    page_url: str = ""
    screenshot_path: str = ""
    error_message: str = ""

    @classmethod
    def from_dict(cls, payload: dict) -> "FailureReport":
        return cls(
            xray_test_key=str(payload.get("xray_test_key", "")).strip(),
            status=str(payload.get("status", "")).strip().lower(),
            summary=str(payload.get("summary", "")).strip(),
            failed_step_no=payload.get("failed_step_no"),
            failed_step_text=str(payload.get("failed_step_text", "")).strip(),
            expected_result=str(payload.get("expected_result", "")).strip(),
            actual_result=str(payload.get("actual_result", "")).strip(),
            page_url=str(payload.get("page_url", "")).strip(),
            screenshot_path=str(payload.get("screenshot_path", "")).strip(),
            error_message=str(payload.get("error_message", "")).strip(),
        )

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def validate(self) -> None:
        if not self.xray_test_key:
            raise ValueError("xray_test_key is required in automation report")
        if self.status not in {"passed", "failed"}:
            raise ValueError("status must be either 'passed' or 'failed'")
        if not self.summary:
            raise ValueError("summary is required in automation report")
        if self.is_failed and not (self.actual_result or self.error_message):
            raise ValueError("actual_result or error_message is required for failed reports")


class JiraBugClient:
    REPORT_START = "BEGIN_AUTOMATION_REPORT"
    REPORT_END = "END_AUTOMATION_REPORT"

    def __init__(self, base_url=None, token=None, email=None, verify_ssl=None, timeout=30, deployment=None):
        self.deployment = (deployment or Config.XRAY_DEPLOYMENT or "datacenter").strip().lower()
        self.is_cloud = self.deployment == "cloud"

        if self.deployment not in {"datacenter", "cloud"}:
            raise ValueError("XRAY_DEPLOYMENT must be either 'datacenter' or 'cloud'")

        if self.is_cloud:
            self.base_url = (base_url or Config.JIRA_CLOUD_URL or "").rstrip("/")
            self.email = email or Config.JIRA_CLOUD_EMAIL
            self.token = token or Config.JIRA_CLOUD_API_TOKEN
            self.verify_ssl = True if verify_ssl is None else verify_ssl
        else:
            self.base_url = (base_url or Config.JIRA_DATACENTER_URL or "").rstrip("/")
            self.email = None
            self.token = token or Config.JIRA_DATACENTER_API_TOKEN
            self.verify_ssl = Config.JIRA_DATACENTER_VERIFY_SSL if verify_ssl is None else verify_ssl

        self.timeout = timeout

        if not self.base_url:
            name = "JIRA_CLOUD_URL" if self.is_cloud else "JIRA_DATACENTER_URL"
            raise ValueError(f"{name} must be configured before using JiraBugClient")
        if self.is_cloud and not self.email:
            raise ValueError("JIRA_CLOUD_EMAIL must be configured before using JiraBugClient")
        if not self.token:
            name = "JIRA_CLOUD_API_TOKEN" if self.is_cloud else "JIRA_DATACENTER_API_TOKEN"
            raise ValueError(f"{name} must be configured before using JiraBugClient")

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _headers(self):
        """Build request headers for the selected Jira deployment."""
        headers = {"Accept": "application/json"}
        if not self.is_cloud:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _auth(self):
        """Return Jira Cloud basic-auth credentials, or None for Data Center bearer auth."""
        if self.is_cloud:
            return (self.email, self.token)
        return None

    def _api_path(self, suffix: str) -> str:
        """Build the Jira REST path using v3 for Cloud and v2 for Data Center."""
        version = "3" if self.is_cloud else "2"
        return f"/rest/api/{version}{suffix}"

    @staticmethod
    def _issue_type_field(issue_type: str) -> dict:
        """
        Build the Jira issue type field from either an id or a name.

        Examples:
            "1" -> {"id": "1"}
            "Bug" -> {"name": "Bug"}
        """
        value = str(issue_type or "").strip()
        if value.isdigit():
            return {"id": value}
        return {"name": value}

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        response = requests.request(
            method,
            url,
            headers={**self._headers(), **kwargs.pop("headers", {})},
            auth=self._auth(),
            timeout=self.timeout,
            verify=self.verify_ssl,
            **kwargs,
        )

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            message = response.text.strip() or "Unexpected non-JSON response"
            if not response.ok:
                raise RuntimeError(f"Request failed for {path} ({response.status_code}): {message}")
            return message

        payload = response.json()
        if not response.ok:
            raise RuntimeError(f"Bug creation request failed for {path} ({response.status_code}): {payload}")
        return payload

    @classmethod
    def parse_automation_report(cls, raw_text: str) -> FailureReport:
        if cls.REPORT_START not in raw_text or cls.REPORT_END not in raw_text:
            raise ValueError("Automation report markers were not found in the agent output")

        json_block = raw_text.split(cls.REPORT_START, 1)[1].split(cls.REPORT_END, 1)[0].strip() # We extract the text between the REPORT_START and REPORT_END markers, which should be a JSON string representing the report. We use split to isolate this block of text, and then strip it to remove any leading or trailing whitespace.
        
        # JSON string -> Python dict -> FailureReport object
        report = FailureReport.from_dict(json.loads(json_block)) # We parse the JSON string into a dictionary using json.loads, and then we create a FailureReport instance from that dictionary using the from_dict class method. This gives us a structured report object that we can work with in Python.
        report.validate() # We call the validate method on the report to ensure that it contains all the required fields and that the values are consistent (e.g., status is either 'passed' or 'failed', summary is not empty, etc.). If the validation fails, it will raise a ValueError with an appropriate message.
        return report # example report is {
                      #     "xray_test_key": "PROJ-123", 
                      #     "status": "failed",
                      #     "summary": "Login button does not respond",
                      #     "failed_step_no": 2,
                      #     "failed_step_text": "Click the login button",
                      #     "expected_result": "The user should be logged in and redirected to the dashboard"
                      #    "actual_result": "Nothing happens when the login button is clicked",
                      #    }
    def create_bug(self, report: FailureReport, project_key: str) -> str:
        project_field = {"id": project_key} if str(project_key).isdigit() else {"key": project_key}

        description = (
            self._build_adf_description(report)
            if self.is_cloud
            else self._build_description(report)
        )
        payload = {
            "fields": {
                "project": project_field,
                "summary": f"[AUTO][{report.xray_test_key}] {report.summary}",
                "description": description,
                "issuetype": self._issue_type_field(Config.BUG_ISSUE_TYPE_ID),
            }
        }
        if Config.BUG_TYPE_FIELD_ID and Config.BUG_TYPE_OPTION_ID:
            payload["fields"][Config.BUG_TYPE_FIELD_ID] = {"id": Config.BUG_TYPE_OPTION_ID}

        issue = self._request(
            "POST",
            self._api_path("/issue"),
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
        )
        issue_key = issue["key"]

        self.link_bug_to_xray_test(issue_key, report.xray_test_key)

        if report.screenshot_path:
            self.attach_file(issue_key, report.screenshot_path)

        return issue_key

    def link_bug_to_xray_test(self, bug_key: str, xray_test_key: str) -> None:
        if not bug_key or not xray_test_key:
            logger.warning(
                "Bug-to-Xray link skipped because bug_key or xray_test_key is missing. "
                "bug_key=%s, xray_test_key=%s",
                bug_key or "N/A",
                xray_test_key or "N/A",
            )
            return

        payload = {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": bug_key},
            "outwardIssue": {"key": xray_test_key},
        }
        try:
            self._request(
                "POST",
                self._api_path("/issueLink"),
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
            )
            logger.info("Linked bug %s to Xray test %s", bug_key, xray_test_key)
        except Exception as exc:
            logger.warning(
                "Linking the bug %s to Xray test %s failed: %s",
                bug_key,
                xray_test_key,
                exc,
            )

    def attach_file(self, issue_key: str, file_path: str) -> None:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return

        with path.open("rb") as handle:
            self._request(
                "POST",
                self._api_path(f"/issue/{issue_key}/attachments"),
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (path.name, handle)},
            )

    @staticmethod
    def _adf_text_node(text: str) -> dict:
        """
        Create an Atlassian Document Format text node.

        ADF is Jira Cloud's JSON representation for rich text fields. A plain
        string such as "Actual result" must be wrapped as a typed node before it
        can be sent inside fields like description.
        """
        return {"type": "text", "text": text or ""}

    @classmethod
    def _adf_paragraph(cls, text: str = "") -> dict:
        """
        Create an ADF paragraph node.

        In Jira Cloud, a normal text line in the description is represented as a
        paragraph. Empty paragraphs are allowed, but this method only adds a text
        child when there is text to display.
        """
        paragraph = {"type": "paragraph", "content": []}
        if text:
            paragraph["content"].append(cls._adf_text_node(text))
        return paragraph

    @classmethod
    def _adf_heading(cls, text: str, level: int = 3) -> dict:
        """
        Create an ADF heading node for section titles.

        The old Data Center description is plain text with section labels such
        as "Actual Results" and "Expected Results". Jira Cloud can render those
        labels more cleanly when they are sent as heading nodes.
        """
        return {
            "type": "heading",
            "attrs": {"level": level},
            "content": [cls._adf_text_node(text)],
        }

    @classmethod
    def _adf_bullet_list(cls, items: list[str]) -> dict:
        """
        Create an ADF bullet list from plain text list items.

        A line like "- Button did not respond" cannot be sent as markdown to
        Jira Cloud REST v3. It must become a bulletList containing listItem
        nodes, and each listItem contains a paragraph node.
        """
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [cls._adf_paragraph(item)],
                }
                for item in items
            ],
        }

    def _build_adf_description(self, report: FailureReport) -> dict:
        """
        Convert the existing plain-text bug description into Jira Cloud ADF.

        ADF means Atlassian Document Format. It is the JSON document structure
        Jira Cloud uses for rich text fields such as description. Data Center can
        accept our existing plain string description, but Jira Cloud REST v3
        expects a JSON document like:

            {
              "type": "doc",
              "version": 1,
              "content": [...]
            }

        To keep both deployments consistent, this method first builds the same
        plain-text description used by Data Center, then maps:
        - known section titles to ADF heading nodes
        - normal lines to ADF paragraph nodes
        - "- " lines to ADF bullet list items
        """
        description_text = self._build_description(report)
        content = []
        pending_bullets = []

        def flush_bullets():
            # Consecutive "- " lines are collected first because ADF represents
            # a list as one bulletList node with many listItem children. As soon
            # as a paragraph, heading, or blank line starts, the current list is
            # complete, so we append it to the document and clear the collector.
            if pending_bullets:
                content.append(self._adf_bullet_list(pending_bullets.copy()))
                pending_bullets.clear()

        section_headings = {
            "Recreation Steps",
            "Actual Results",
            "Expected Results",
            "Attachments",
            "Recovery Status",
            "Recreation Rate",
            "Technical Details",
        }

        for line in description_text.splitlines():
            stripped = line.strip()
            if not stripped:
                flush_bullets()
                continue
            if stripped in section_headings:
                flush_bullets()
                content.append(self._adf_heading(stripped))
            elif stripped.startswith("- "):
                pending_bullets.append(stripped[2:].strip())
            else:
                flush_bullets()
                content.append(self._adf_paragraph(stripped))

        flush_bullets()

        return {
            "type": "doc",
            "version": 1,
            "content": content or [self._adf_paragraph("No description provided.")],
        }

    def _build_description(self, report: FailureReport) -> str:
        def _clean_display_text(text: str) -> str:
            if not text:
                return ""
            cleaned = re.sub(r"^\s*\d+\s*[\.\):-]\s*", "", text.strip()) # This regex removes common numbering or bullet patterns from the beginning of the text, such as "1. ", "2) ", "- ", "3: ", etc. It looks for optional whitespace, followed by one or more digits, optional whitespace, an optional separator (., ), :, or -), and then more optional whitespace before the actual text starts. This helps to clean up the step text for better readability in the bug description.
            return cleaned.strip()

        recreation_steps = []
        failed_step_text = _clean_display_text(report.failed_step_text)
        actual_result = _clean_display_text(report.actual_result) or _clean_display_text(report.error_message)
        expected_result = _clean_display_text(report.expected_result)

        if failed_step_text:
            recreation_steps.append(failed_step_text)
        else:
            recreation_steps.append("See Xray scenario for the exact executed steps.")
        actual_result = actual_result or "Actual result was not provided."
        expected_result = expected_result or "Expected result was not provided."
        attachments = "Screenshot attached." if report.screenshot_path else "None"

        lines = [
            f"Summary: {report.summary}", 
            "",
            "Recreation Steps",
            "",
            *recreation_steps,
            "",
            "Actual Results",
            "",
            f"- {actual_result}",
            "",
            "Expected Results",
            "",
            f"- {expected_result}",
            "",
            "Attachments",
            "",
            attachments,
            "",
            "Recovery Status",
            "",
            "None",
            "",
            "Recreation Rate",
            "",
            "Unknown",
            "",
            "Technical Details",
            "",
            f"- Source Xray Test: {report.xray_test_key}",
            f"- Execution Status: {report.status}",
            f"- Page URL: {report.page_url or 'N/A'}",
            f"- Created At (UTC): {datetime.now(timezone.utc).isoformat()}",
        ]

        return os.linesep.join(lines)
