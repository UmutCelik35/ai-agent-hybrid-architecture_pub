import os
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

# Appearance & theme
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Color palette
BG_DARK = "#0f1117"
BG_PANEL = "#16181f"
BG_CARD = "#1e2130"
ACCENT = "#4f8ef7"
ACCENT2 = "#7c5cbf"
SUCCESS = "#3ecf8e"
WARNING = "#f5a623"
DANGER = "#e05252"
TEXT_PRI = "#e8eaf0"
TEXT_SEC = "#7b8094"
BORDER = "#2a2d3a"

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
XRAY_SCRIPT = PROJECT_ROOT / "Web_Aut" / "playwright_xray_execution.py"
XRAY_SINGLE_TEST_SCRIPT = PROJECT_ROOT / "Web_Aut" / "playwright_xray.py"
ENV_FILE = PROJECT_ROOT / ".env"
sys.path.insert(0, str(PROJECT_ROOT))

from Bug_Review.review_launcher import launch_review_server
from LLMs.modelClient import ModelClient
from Resources.config import Config
from Static_Aut.profiles.profile_manager import (
    default_locator_file_name,
    delete_profile,
    get_profile,
    list_locator_files,
    list_profile_names,
    save_profile,
)

USE_ENV_MODEL_LABEL = "Use .env default"
RUN_TYPE_EXECUTION = "Test Execution"
RUN_TYPE_SINGLE_TEST = "Single Test"
XRAY_DEPLOYMENT_DATA_CENTER = "Data Center"
XRAY_DEPLOYMENT_CLOUD = "Cloud"
XRAY_DEPLOYMENT_VALUES = {
    XRAY_DEPLOYMENT_DATA_CENTER: "datacenter",
    XRAY_DEPLOYMENT_CLOUD: "cloud",
}
BUG_CREATION_MODES = ["review", "auto", "off"]
STATIC_TOOLBOX_MODES = ["on", "shadow", "off"]
BOOL_VALUES = ["true", "false"]
DATACENTER_BUG_TYPE_FIELD_ID = "customfield_11953"
DATACENTER_BUG_TYPE_OPTION_ID = "11502"


class XrayRunnerApp(ctk.CTk):
    """Main application window for the Xray Automation Runner UI."""

    def __init__(self):
        super().__init__()
        self.title("Web Automation Runner")
        self.geometry("860x640")
        self.minsize(1080, 900)
        self.configure(fg_color=BG_DARK)
        self.resizable(True, True)

        self._process: subprocess.Popen | None = None
        self._model_aliases = list(ModelClient.available_models().keys())
        self._profile_names = list_profile_names()

        self._build_ui()

    def _build_ui(self):
        """Build and lay out all UI widgets."""
        header = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text="Web Automation Runner",
            font=ctk.CTkFont(family="Courier New", size=20, weight="bold"),
            text_color=ACCENT,
        ).pack(side="left", padx=24, pady=16)

        self._status_dot = ctk.CTkLabel(
            header,
            text="Ready",
            font=ctk.CTkFont(size=13),
            text_color=SUCCESS,
        )
        self._status_dot.pack(side="right", padx=24)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(16, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        card = ctk.CTkScrollableFrame(
            body,
            fg_color=BG_CARD,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
            height=390,
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        card.columnconfigure(1, weight=1)

        self._add_label(card, "Run Type", row=0)
        self._run_type_var = ctk.StringVar(value=RUN_TYPE_EXECUTION)
        run_type_menu = ctk.CTkOptionMenu(
            card,
            variable=self._run_type_var,
            values=[RUN_TYPE_EXECUTION, RUN_TYPE_SINGLE_TEST],
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
        )
        run_type_menu.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._add_label(card, "Xray Deployment", row=1)
        self._xray_deployment_var = ctk.StringVar(value=self._default_xray_deployment_label())
        xray_deployment_menu = ctk.CTkOptionMenu(
            card,
            variable=self._xray_deployment_var,
            values=list(XRAY_DEPLOYMENT_VALUES.keys()),
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
            command=lambda _value: self._toggle_deployment_fields(),
        )
        xray_deployment_menu.grid(row=1, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._add_label(card, "Xray Key", row=2)
        self._exec_key_var = ctk.StringVar()
        exec_entry = ctk.CTkEntry(
            card,
            textvariable=self._exec_key_var,
            placeholder_text="e.g. PROJ-356 or PROJ-476",
            font=ctk.CTkFont(family="Courier New", size=14),
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_PRI,
            placeholder_text_color=TEXT_SEC,
        )
        exec_entry.grid(row=2, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._add_label(card, "LLM Model", row=3)
        self._model_var = ctk.StringVar(value=self._default_model_value())
        model_menu = ctk.CTkOptionMenu(
            card,
            variable=self._model_var,
            values=[USE_ENV_MODEL_LABEL, *self._model_aliases],
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
        )
        model_menu.grid(row=3, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._add_label(card, "OpenAI Key", row=4)
        self._openai_key_var = ctk.StringVar()
        openai_key_entry = ctk.CTkEntry(
            card,
            textvariable=self._openai_key_var,
            placeholder_text="Leave empty to use .env",
            show="*",
            font=ctk.CTkFont(family="Courier New", size=14),
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_PRI,
            placeholder_text_color=TEXT_SEC,
        )
        openai_key_entry.grid(row=4, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._add_label(card, "Google Key", row=5)
        self._google_key_var = ctk.StringVar()
        google_key_entry = ctk.CTkEntry(
            card,
            textvariable=self._google_key_var,
            placeholder_text="Leave empty to use .env",
            show="*",
            font=ctk.CTkFont(family="Courier New", size=14),
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_PRI,
            placeholder_text_color=TEXT_SEC,
        )
        google_key_entry.grid(row=5, column=1, sticky="ew", padx=(0, 20), pady=8)

        self._field_rows = {}

        self._jira_datacenter_url_var = ctk.StringVar(value=Config.JIRA_DATACENTER_URL or "")
        self._add_entry_field(
            card,
            "Jira Data Center URL",
            self._jira_datacenter_url_var,
            row=6,
            placeholder="https://jira.example.com",
            field_key="datacenter",
        )

        self._jira_datacenter_token_var = ctk.StringVar()
        self._add_entry_field(
            card,
            "Jira Data Center Token",
            self._jira_datacenter_token_var,
            row=7,
            placeholder="Leave empty to use .env",
            field_key="datacenter",
            secret=True,
        )

        self._jira_cloud_url_var = ctk.StringVar(value=Config.JIRA_CLOUD_URL or "")
        self._add_entry_field(
            card,
            "Jira Cloud URL",
            self._jira_cloud_url_var,
            row=8,
            placeholder="https://your-domain.atlassian.net",
            field_key="cloud",
        )

        self._jira_cloud_email_var = ctk.StringVar(value=Config.JIRA_CLOUD_EMAIL or "")
        self._add_entry_field(
            card,
            "Jira Cloud Email",
            self._jira_cloud_email_var,
            row=9,
            placeholder="name@example.com",
            field_key="cloud",
        )

        self._jira_cloud_token_var = ctk.StringVar()
        self._add_entry_field(
            card,
            "Jira Cloud Token",
            self._jira_cloud_token_var,
            row=10,
            placeholder="Leave empty to use .env",
            field_key="cloud",
            secret=True,
        )

        self._xray_cloud_api_url_var = ctk.StringVar(value=Config.XRAY_CLOUD_API_URL or "https://xray.cloud.getxray.app")
        self._add_entry_field(
            card,
            "Xray Cloud API",
            self._xray_cloud_api_url_var,
            row=11,
            placeholder="https://xray.cloud.getxray.app",
            field_key="cloud",
        )

        self._xray_cloud_client_id_var = ctk.StringVar(value=Config.XRAY_CLOUD_CLIENT_ID or "")
        self._add_entry_field(
            card,
            "Xray Client ID",
            self._xray_cloud_client_id_var,
            row=12,
            placeholder="Xray Cloud client id",
            field_key="cloud",
        )

        self._xray_cloud_client_secret_var = ctk.StringVar()
        self._add_entry_field(
            card,
            "Xray Client Secret",
            self._xray_cloud_client_secret_var,
            row=13,
            placeholder="Leave empty to use .env",
            field_key="cloud",
            secret=True,
        )

        self._bug_project_key_var = ctk.StringVar(value=Config.BUG_PROJECT_KEY or "")
        self._add_entry_field(
            card,
            "Bug Project Key",
            self._bug_project_key_var,
            row=14,
            placeholder="e.g. PROJ",
        )

        self._bug_creation_mode_var = ctk.StringVar(value=(Config.BUG_CREATION_MODE or "review").strip().lower() or "review")
        self._add_option_field(card, "Bug Mode", self._bug_creation_mode_var, BUG_CREATION_MODES, row=15)

        self._static_toolbox_mode_var = ctk.StringVar(value=(Config.STATIC_TOOLBOX_MODE or "on").strip().lower() or "on")
        self._add_option_field(card, "Static Mode", self._static_toolbox_mode_var, STATIC_TOOLBOX_MODES, row=16)

        self._static_headless_var = ctk.StringVar(value=self._bool_text(Config.STATIC_PLAYWRIGHT_HEADLESS, default=False))
        self._add_option_field(card, "Headless", self._static_headless_var, BOOL_VALUES, row=17)

        self._static_self_healing_var = ctk.StringVar(value=self._bool_text(Config.STATIC_SELF_HEALING_ENABLED, default=True))
        self._add_option_field(card, "Self Healing", self._static_self_healing_var, BOOL_VALUES, row=18)

        self._static_llm_router_var = ctk.StringVar(value=self._bool_text(Config.STATIC_LLM_ROUTER_ENABLED, default=True))
        self._add_option_field(card, "LLM Router", self._static_llm_router_var, BOOL_VALUES, row=19)

        self._static_tool_suggestions_var = ctk.StringVar(value=self._bool_text(Config.STATIC_TOOL_SUGGESTIONS_ENABLED, default=True))
        self._add_option_field(card, "Tool Suggestions", self._static_tool_suggestions_var, BOOL_VALUES, row=20)

        self._static_default_app_name_var = ctk.StringVar(value=self._default_profile_value())
        self._profile_menu = self._add_profile_field(
            card,
            "App Profile",
            self._static_default_app_name_var,
            row=21,
        )

        self._save_env_var = ctk.BooleanVar(value=False)
        save_env_checkbox = ctk.CTkCheckBox(
            card,
            text="Save entered configuration to .env",
            variable=self._save_env_var,
            font=ctk.CTkFont(size=13),
            text_color=TEXT_PRI,
            fg_color=ACCENT,
            hover_color=ACCENT2,
        )
        save_env_checkbox.grid(row=22, column=1, sticky="w", padx=(0, 20), pady=8)

        self._toggle_deployment_fields()

        log_frame = ctk.CTkFrame(
            body,
            fg_color=BG_CARD,
            corner_radius=12,
            border_width=1,
            border_color=BORDER,
        )
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(log_frame, fg_color="transparent", height=36)
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 0))
        ctk.CTkLabel(
            log_header,
            text="Console Output",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=TEXT_SEC,
        ).pack(side="left")
        ctk.CTkButton(
            log_header,
            text="Clear",
            width=70,
            height=26,
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            hover_color=BG_DARK,
            text_color=TEXT_SEC,
            border_width=1,
            border_color=BORDER,
            command=self._clear_log,
        ).pack(side="right")

        self._log = ctk.CTkTextbox(
            log_frame,
            font=ctk.CTkFont(family="Courier New", size=12),
            fg_color="transparent",
            text_color="#a8d8a8",
            activate_scrollbars=True,
            wrap="word",
        )
        self._log.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=12, pady=(4, 12))
        self._log.configure(state="disabled")

        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=64)
        btn_bar.pack(fill="x", side="bottom")
        btn_bar.pack_propagate(False)

        self._stop_btn = ctk.CTkButton(
            btn_bar,
            text="Stop",
            width=120,
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=DANGER,
            hover_color="#b03c3c",
            command=self._stop_run,
            state="disabled",
        )
        self._stop_btn.pack(side="right", padx=(8, 20), pady=13)

        self._run_btn = ctk.CTkButton(
            btn_bar,
            text="Run",
            width=140,
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT,
            hover_color=ACCENT2,
            command=self._start_run,
        )
        self._run_btn.pack(side="right", padx=0, pady=13)

        self._open_log_btn = ctk.CTkButton(
            btn_bar,
            text="Open Log",
            width=120,
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=ACCENT2,
            hover_color=ACCENT,
            command=self._open_log_file,
        )
        self._open_log_btn.pack(side="right", padx=(0, 8), pady=13)

        self._open_report_btn = ctk.CTkButton(
            btn_bar,
            text="Open Last Review",
            width=150,
            height=38,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=BG_PANEL,
            hover_color=BG_DARK,
            text_color=TEXT_SEC,
            border_width=1,
            border_color=BORDER,
            command=self._open_last_review,
        )
        self._open_report_btn.pack(side="right", padx=(0, 8), pady=13)

    def _open_log_file(self):
        """Open the logs/automation.log file with the default system application."""
        log_path = PROJECT_ROOT / "logs" / "automation.log"
        if not log_path.exists():
            self._log_write(f"\nERROR: Log file not found: {log_path}\n")
            return
        self._open_path(log_path, "log file")

    def _open_last_review(self):
        """Open the newest interactive bug review UI if a report JSON exists."""
        report_json_path = self._find_latest_report_json()
        if not report_json_path:
            self._log_write("\nERROR: No review report JSON was found under logs/execution_reports.\n")
            return
        self._log_write(f"Opening review UI: {report_json_path.relative_to(PROJECT_ROOT).as_posix()}\n")
        opened = launch_review_server(
            report_json_path,
            require_auto_open=False,
            require_review_mode=False,
            require_pending_candidates=False,
        )
        if not opened:
            self._log_write("ERROR: Review UI could not be opened. Check the review server log next to the report JSON.\n")

    def _find_latest_report_json(self) -> Path | None:
        report_dir = PROJECT_ROOT / "logs" / "execution_reports"
        if not report_dir.exists():
            return None
        report_files = [path for path in report_dir.glob("*.json") if path.is_file()]
        if not report_files:
            return None
        return max(report_files, key=lambda path: path.stat().st_mtime)

    def _add_label(self, parent, text, row):
        """Create and grid a right-aligned label in the settings card."""
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=13),
            text_color=TEXT_SEC,
            anchor="e",
        )
        lbl.grid(row=row, column=0, sticky="e", padx=(20, 16), pady=8)
        return lbl

    def _add_entry_field(
        self,
        parent,
        label: str,
        variable: ctk.StringVar,
        row: int,
        placeholder: str = "",
        field_key: str = "",
        secret: bool = False,
    ):
        label_widget = self._add_label(parent, label, row=row)
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            placeholder_text=placeholder,
            show="*" if secret else "",
            font=ctk.CTkFont(family="Courier New", size=14),
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_PRI,
            placeholder_text_color=TEXT_SEC,
        )
        entry.grid(row=row, column=1, sticky="ew", padx=(0, 20), pady=8)
        if field_key:
            self._field_rows.setdefault(field_key, []).append((label_widget, entry, row))
        return entry

    def _add_option_field(
        self,
        parent,
        label: str,
        variable: ctk.StringVar,
        values: list[str],
        row: int,
    ):
        self._add_label(parent, label, row=row)
        menu = ctk.CTkOptionMenu(
            parent,
            variable=variable,
            values=values,
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
        )
        menu.grid(row=row, column=1, sticky="ew", padx=(0, 20), pady=8)
        return menu

    def _add_profile_field(
        self,
        parent,
        label: str,
        variable: ctk.StringVar,
        row: int,
    ):
        self._add_label(parent, label, row=row)
        field_frame = ctk.CTkFrame(parent, fg_color="transparent")
        field_frame.grid(row=row, column=1, sticky="ew", padx=(0, 20), pady=8)
        field_frame.columnconfigure(0, weight=1)
        values = self._profile_names or [""]
        menu = ctk.CTkOptionMenu(
            field_frame,
            variable=variable,
            values=values,
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
        )
        menu.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            field_frame,
            text="Manage",
            width=96,
            height=30,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT2,
            hover_color=ACCENT,
            command=self._open_profile_manager,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))
        return menu

    def _default_model_value(self) -> str:
        return USE_ENV_MODEL_LABEL

    def _default_profile_value(self) -> str:
        configured = (Config.STATIC_DEFAULT_APP_NAME or "").strip()
        if configured in self._profile_names:
            return configured
        if configured:
            return configured
        return self._profile_names[0] if self._profile_names else ""

    def _default_xray_deployment_label(self) -> str:
        deployment = (Config.XRAY_DEPLOYMENT or "datacenter").strip().lower()
        for label, value in XRAY_DEPLOYMENT_VALUES.items():
            if value == deployment:
                return label
        return XRAY_DEPLOYMENT_DATA_CENTER

    def _bool_text(self, value: bool, default: bool) -> str:
        return "true" if bool(value if value is not None else default) else "false"

    def _toggle_deployment_fields(self):
        deployment = XRAY_DEPLOYMENT_VALUES.get(self._xray_deployment_var.get(), "datacenter")
        for field_key, widgets in self._field_rows.items():
            should_show = field_key == deployment
            for label_widget, input_widget, row in widgets:
                if should_show:
                    label_widget.grid(row=row, column=0, sticky="e", padx=(20, 16), pady=8)
                    input_widget.grid(row=row, column=1, sticky="ew", padx=(0, 20), pady=8)
                else:
                    label_widget.grid_remove()
                    input_widget.grid_remove()

    def _refresh_profiles(self, selected: str = "") -> None:
        self._profile_names = list_profile_names()
        values = self._profile_names or [""]
        if hasattr(self, "_profile_menu"):
            self._profile_menu.configure(values=values)
        selected = selected.strip()
        if selected and selected in self._profile_names:
            self._static_default_app_name_var.set(selected)
        elif self._static_default_app_name_var.get().strip() not in self._profile_names:
            self._static_default_app_name_var.set(self._profile_names[0] if self._profile_names else "")

    def _open_profile_manager(self):
        ProfileManagerWindow(self)

    def _save_values_to_env(
        self,
        updates: dict[str, str],
    ) -> None:
        if not updates:
            self._log_write(
                "Save to .env skipped: no explicit configuration values were provided. "
                "Only explicit overrides are persisted.\n"
            )
            return

        lines = []
        if ENV_FILE.exists():
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in updates and stripped and not stripped.startswith("#"):
                new_lines.append(f'{key}="{updates[key]}"')
                updated_keys.add(key)
            else:
                new_lines.append(line)

        for key, value in updates.items():
            if key not in updated_keys:
                new_lines.append(f'{key}="{value}"')

        ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        saved_keys = ", ".join(updates.keys())
        self._log_write(f"Saved to .env: {saved_keys}\n")

    def _collect_env_updates(self, xray_deployment: str, model_override: str) -> dict[str, str]:
        updates = {
            "XRAY_DEPLOYMENT": xray_deployment,
            "BUG_PROJECT_KEY": self._bug_project_key_var.get().strip(),
            "BUG_CREATION_MODE": self._bug_creation_mode_var.get().strip(),
            "STATIC_TOOLBOX_MODE": self._static_toolbox_mode_var.get().strip() or "on",
            "STATIC_PLAYWRIGHT_HEADLESS": self._static_headless_var.get().strip() or "false",
            "STATIC_SELF_HEALING_ENABLED": self._static_self_healing_var.get().strip() or "true",
            "STATIC_LLM_ROUTER_ENABLED": self._static_llm_router_var.get().strip() or "true",
            "STATIC_TOOL_SUGGESTIONS_ENABLED": self._static_tool_suggestions_var.get().strip() or "true",
            "STATIC_DEFAULT_APP_NAME": self._static_default_app_name_var.get().strip(),
        }
        if model_override:
            updates["LLM_MODEL_NAME"] = model_override
        if self._openai_key_var.get().strip():
            updates["OPENAI_API_KEY"] = self._openai_key_var.get().strip()
        if self._google_key_var.get().strip():
            updates["GOOGLE_API_KEY"] = self._google_key_var.get().strip()

        if xray_deployment == "datacenter":
            updates["JIRA_DATACENTER_URL"] = self._jira_datacenter_url_var.get().strip()
            if self._jira_datacenter_token_var.get().strip():
                updates["JIRA_DATACENTER_API_TOKEN"] = self._jira_datacenter_token_var.get().strip()
            updates["BUG_TYPE_FIELD_ID"] = DATACENTER_BUG_TYPE_FIELD_ID
            updates["BUG_TYPE_OPTION_ID"] = DATACENTER_BUG_TYPE_OPTION_ID
        else:
            updates["JIRA_CLOUD_URL"] = self._jira_cloud_url_var.get().strip()
            updates["JIRA_CLOUD_EMAIL"] = self._jira_cloud_email_var.get().strip()
            if self._jira_cloud_token_var.get().strip():
                updates["JIRA_CLOUD_API_TOKEN"] = self._jira_cloud_token_var.get().strip()
            updates["XRAY_CLOUD_API_URL"] = self._xray_cloud_api_url_var.get().strip() or "https://xray.cloud.getxray.app"
            updates["XRAY_CLOUD_CLIENT_ID"] = self._xray_cloud_client_id_var.get().strip()
            if self._xray_cloud_client_secret_var.get().strip():
                updates["XRAY_CLOUD_CLIENT_SECRET"] = self._xray_cloud_client_secret_var.get().strip()
            updates["BUG_TYPE_FIELD_ID"] = ""
            updates["BUG_TYPE_OPTION_ID"] = ""

        return {key: value for key, value in updates.items() if value is not None}


    def _apply_runtime_env(self, env: dict[str, str], updates: dict[str, str]) -> None:
        for key, value in updates.items():
            env[key] = value

    def _validate_gui_config(self, xray_deployment: str, updates: dict[str, str]) -> bool:
        required = ["BUG_PROJECT_KEY", "STATIC_DEFAULT_APP_NAME"]
        if xray_deployment == "datacenter":
            required.extend(["JIRA_DATACENTER_URL"])
            if not updates.get("JIRA_DATACENTER_API_TOKEN") and not Config.JIRA_DATACENTER_API_TOKEN:
                required.append("JIRA_DATACENTER_API_TOKEN")
        else:
            required.extend(["JIRA_CLOUD_URL", "JIRA_CLOUD_EMAIL", "XRAY_CLOUD_API_URL", "XRAY_CLOUD_CLIENT_ID"])
            if not updates.get("JIRA_CLOUD_API_TOKEN") and not Config.JIRA_CLOUD_API_TOKEN:
                required.append("JIRA_CLOUD_API_TOKEN")
            if not updates.get("XRAY_CLOUD_CLIENT_SECRET") and not Config.XRAY_CLOUD_CLIENT_SECRET:
                required.append("XRAY_CLOUD_CLIENT_SECRET")

        missing = [key for key in required if not str(updates.get(key) or "").strip()]
        if missing:
            self._set_status("Missing config", WARNING)
            self._log_write(f"ERROR: Missing required configuration: {', '.join(missing)}\n")
            return False
        return True

    def _log_write(self, text: str, color: str | None = None):
        """Append text to the log panel and auto-scroll to the bottom."""
        self._log.configure(state="normal")
        self._log.insert("end", text)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _clear_log(self):
        """Clear all content from the log panel."""
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _start_run(self):
        """Validate inputs, build the command, and stream subprocess output."""
        exec_key = self._exec_key_var.get().strip()
        mode = "web"
        run_type = self._run_type_var.get().strip()
        xray_deployment_label = self._xray_deployment_var.get().strip()
        xray_deployment = XRAY_DEPLOYMENT_VALUES.get(xray_deployment_label, "datacenter")
        selected_model = self._model_var.get().strip()
        model_override = selected_model if selected_model != USE_ENV_MODEL_LABEL else ""
        env_updates = self._collect_env_updates(xray_deployment, model_override)

        if not exec_key:
            self._set_status("Execution Key is empty!", WARNING)
            self._log_write("ERROR: Please enter an Xray key.\n")
            return
        if not self._validate_gui_config(xray_deployment, env_updates):
            return

        self._run_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._set_status("Running...", WARNING)
        self._clear_log()
        model_summary = model_override or f".env default ({Config.LLM_MODEL_NAME or 'not set'})"
        if mode != "web":
            self._set_status("Unsupported execution mode", DANGER)
            self._log_write(f"ERROR: Unsupported execution mode '{mode}'. Only 'web' is supported.\n")
            self._run_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
            return

        self._log_write(
            f"Starting - run_type={run_type}, deployment={xray_deployment}, "
            f"mode={mode}, key={exec_key}, model={model_summary}\n"
        )

        if run_type == RUN_TYPE_SINGLE_TEST:
            command = [
                sys.executable,
                str(XRAY_SINGLE_TEST_SCRIPT),
                "--test-key",
                exec_key,
                "--xray-deployment",
                xray_deployment,
            ]
        else:
            command = [
                sys.executable,
                str(XRAY_SCRIPT),
                "--execution-key",
                exec_key,
                "--execution_mode",
                mode,
                "--xray-deployment",
                xray_deployment,
            ]

        self._log_write(f"$ {' '.join(command)}\n\n")
        if self._save_env_var.get():
            try:
                self._save_values_to_env(env_updates)
            except Exception as exc:
                self._set_status("Could not save .env", DANGER)
                self._log_write(f"ERROR: Could not save .env: {exc}\n")
                self._run_btn.configure(state="normal")
                self._stop_btn.configure(state="disabled")
                return

        env = os.environ.copy()
        self._apply_runtime_env(env, env_updates)

        def run():
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                for line in self._process.stdout:
                    self.after(0, self._log_write, line)
                self._process.wait()
                self.after(0, self._on_run_done, self._process.returncode)
            except Exception as exc:
                self.after(0, self._log_write, f"\nERROR: {exc}\n")
                self.after(0, self._on_run_done, -1)

        threading.Thread(target=run, daemon=True).start()

    def _stop_run(self):
        """Terminate the running subprocess if one is active."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._log_write("\nStopped by user.\n")
        self._on_run_done(None)

    def _on_run_done(self, returncode):
        """Reset the UI to idle state and update the status label."""
        self._run_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._process = None

        if returncode is None:
            self._set_status("Stopped", TEXT_SEC)
        elif returncode == 0:
            self._set_status("Success", SUCCESS)
            self._log_write("\nTest execution completed (exit 0).\n")
        else:
            self._set_status(f"Failed (exit {returncode})", DANGER)
            self._log_write(f"\nTest execution failed (exit {returncode}).\n")

    def _set_status(self, text: str, color: str):
        """Update the status label text and color in the header bar."""
        self._status_dot.configure(text=text, text_color=color)


class ProfileManagerWindow(ctk.CTkToplevel):
    def __init__(self, app: XrayRunnerApp):
        super().__init__(app)
        self.app = app
        self.title("Manage App Profiles")
        self.geometry("620x330")
        self.minsize(560, 320)
        self.configure(fg_color=BG_DARK)
        self.transient(app)
        self.grab_set()

        self._profile_var = ctk.StringVar(value=app._static_default_app_name_var.get().strip())
        self._name_var = ctk.StringVar(value=self._profile_var.get())
        profile = get_profile(self._name_var.get())
        self._locator_var = ctk.StringVar(
            value=profile.locator_file if profile else default_locator_file_name(self._name_var.get())
        )
        self._status_var = ctk.StringVar(value="")

        self._build()
        self._load_selected_profile()

    def _build(self):
        frame = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=12, border_width=1, border_color=BORDER)
        frame.pack(fill="both", expand=True, padx=18, pady=18)
        frame.columnconfigure(1, weight=1)

        self._add_label(frame, "Existing", 0)
        self._profile_menu = ctk.CTkOptionMenu(
            frame,
            variable=self._profile_var,
            values=self._profile_values(),
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
            command=lambda _value: self._load_selected_profile(),
        )
        self._profile_menu.grid(row=0, column=1, sticky="ew", padx=(0, 18), pady=10)

        self._add_label(frame, "Name", 1)
        name_entry = ctk.CTkEntry(
            frame,
            textvariable=self._name_var,
            font=ctk.CTkFont(family="Courier New", size=14),
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_PRI,
        )
        name_entry.grid(row=1, column=1, sticky="ew", padx=(0, 18), pady=10)

        self._add_label(frame, "Locator File", 2)
        self._locator_box = ctk.CTkComboBox(
            frame,
            variable=self._locator_var,
            values=self._locator_values(),
            font=ctk.CTkFont(family="Courier New", size=13),
            fg_color=BG_PANEL,
            border_color=BORDER,
            button_color=ACCENT,
            button_hover_color=ACCENT2,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=BG_DARK,
            dropdown_text_color=TEXT_PRI,
            text_color=TEXT_PRI,
        )
        self._locator_box.grid(row=2, column=1, sticky="ew", padx=(0, 18), pady=10)

        self._status = ctk.CTkLabel(
            frame,
            textvariable=self._status_var,
            font=ctk.CTkFont(size=12),
            text_color=TEXT_SEC,
            anchor="w",
        )
        self._status.grid(row=3, column=1, sticky="ew", padx=(0, 18), pady=(4, 8))

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=4, column=1, sticky="e", padx=(0, 18), pady=(18, 8))

        ctk.CTkButton(
            actions,
            text="New",
            width=76,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BG_PANEL,
            hover_color=BG_DARK,
            border_width=1,
            border_color=BORDER,
            command=self._new_profile,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Use Selected",
            width=112,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=BG_PANEL,
            hover_color=BG_DARK,
            border_width=1,
            border_color=BORDER,
            command=self._use_selected,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Save",
            width=88,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT,
            hover_color=ACCENT2,
            command=self._save,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Delete",
            width=88,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=DANGER,
            hover_color="#b03c3c",
            command=self._delete,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Close",
            width=88,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=ACCENT2,
            hover_color=ACCENT,
            command=self.destroy,
        ).pack(side="left")

    def _add_label(self, parent, text: str, row: int):
        label = ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=13), text_color=TEXT_SEC, anchor="e")
        label.grid(row=row, column=0, sticky="e", padx=(18, 16), pady=10)

    def _profile_values(self) -> list[str]:
        return list_profile_names() or [""]

    def _locator_values(self) -> list[str]:
        values = list_locator_files()
        current = self._locator_var.get().strip()
        if current and current not in values:
            values.insert(0, current)
        return values or [current or ""]

    def _load_selected_profile(self):
        selected = self._profile_var.get().strip()
        profile = get_profile(selected)
        if not profile:
            return
        self._name_var.set(profile.app_name)
        self._locator_var.set(profile.locator_file)
        self._status_var.set("")

    def _save(self):
        try:
            profile = save_profile(self._name_var.get(), self._locator_var.get(), create_locator=True)
        except Exception as exc:
            self._status.configure(text_color=DANGER)
            self._status_var.set(str(exc))
            return
        self._refresh_controls(profile.app_name)
        self.app._refresh_profiles(profile.app_name)
        self._status.configure(text_color=SUCCESS)
        self._status_var.set(f"Saved profile: {profile.app_name}")

    def _delete(self):
        name = self._name_var.get().strip()
        if not name:
            self._status.configure(text_color=DANGER)
            self._status_var.set("Select a profile to delete")
            return
        if not messagebox.askyesno("Delete Profile", f"Delete profile '{name}'?", parent=self):
            return
        try:
            delete_profile(name)
        except Exception as exc:
            self._status.configure(text_color=DANGER)
            self._status_var.set(str(exc))
            return
        self._refresh_controls("")
        self.app._refresh_profiles("")
        self._status.configure(text_color=WARNING)
        self._status_var.set(f"Deleted profile: {name}")

    def _use_selected(self):
        name = self._name_var.get().strip()
        if not get_profile(name):
            self._status.configure(text_color=DANGER)
            self._status_var.set("Save the profile before selecting it")
            return
        self.app._refresh_profiles(name)
        self._status.configure(text_color=SUCCESS)
        self._status_var.set(f"Selected profile: {name}")

    def _new_profile(self):
        self._profile_var.set("")
        self._name_var.set("")
        self._locator_var.set("")
        self._status.configure(text_color=TEXT_SEC)
        self._status_var.set("")

    def _refresh_controls(self, selected: str):
        profiles = self._profile_values()
        self._profile_menu.configure(values=profiles)
        self._locator_box.configure(values=self._locator_values())
        if selected:
            self._profile_var.set(selected)
            self._load_selected_profile()
        elif profiles and profiles[0]:
            self._profile_var.set(profiles[0])
            self._load_selected_profile()
        else:
            self._profile_var.set("")
            self._name_var.set("")
            self._locator_var.set("")


if __name__ == "__main__":
    app = XrayRunnerApp()
    app.mainloop()
