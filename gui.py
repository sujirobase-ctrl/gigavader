"""Modern desktop interface for the BUIDL thread downloader."""

from __future__ import annotations

import os
import queue
import threading
import time
import tempfile
from datetime import datetime
from pathlib import Path, PureWindowsPath
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk

from src import __version__
from src.config import INTER_FILE_DELAY, MAX_RETRIES, MAX_WORKERS, TaskResult
from src.downloaders import ThreadDownloader, ThreadOptions
from src.file_utils import redact_sensitive_urls
from src.http_client import HttpClient
from src.managers.log_manager import LoggerTable
from src.managers.summary_manager import SummaryManager
from src.preview import PreviewCommand, PreviewSession, preview_fingerprint
from src.preview_gui import PreviewWindow
from src.recovery import RecoveryStore

APP_NAME = "BUIDL"
ACCENT = "#6C63FF"
ACCENT_HOVER = "#817AFF"
SURFACE = "#151823"
SURFACE_ALT = "#1C2030"
TEXT_MUTED = "#8E95A9"
SUCCESS = "#2ECC8F"
WARNING = "#F2B84B"
DANGER = "#F06565"

FAILURE_EVENTS = {
    "Download failed",
    "Unexpected error",
    "Page error",
    "Access denied",
    "Login required",
}


def default_download_directory() -> Path:
    """Use the user's files, independent of the app that launched the EXE."""
    return Path.home() / "Downloads" / "BUIDL"


def validate_output_directory(value: str) -> Path:
    folder = Path(value).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(prefix=".buidl-write-check-", dir=folder):
            pass
    except OSError as error:
        raise ValueError("Não foi possível gravar na pasta de destino. Escolha uma pasta sua, como Downloads/BUIDL.") from error
    return folder


def recovery_output_directory(document) -> str:
    options = document["options"]
    saved = options.get("download_path") or options.get("custom_path") or ""
    saved_parts = {*Path(saved).parts, *PureWindowsPath(saved).parts}
    if not document.get("prepared") and "windowsapps" in {part.lower() for part in saved_parts}:
        # Earlier builds inherited the launcher's protected working directory.
        # No prepared queue exists here, so no downloaded files are relocated.
        saved = str(default_download_directory())
        options["download_path"], options["custom_path"] = saved, None
    return saved


class GuiManager:
    """Translate downloader events into thread-safe GUI messages."""

    def __init__(self, events: queue.Queue, log_file: Path) -> None:  # noqa: ANN001
        self.events = events
        self.logger = LoggerTable(log_file=str(log_file), verbose=False)
        self.summary = SummaryManager()
        self.total = 0
        self.title = ""
        self._task_id = 0
        self._lock = threading.Lock()
        self.start_time = time.monotonic()

    def start(self) -> None:
        self.update_log(event="Iniciado", details="Preparando o download.")

    def stop(self) -> None:
        elapsed = time.monotonic() - self.start_time
        counts = self._counts()
        self.update_log(
            event="Resumo",
            details=(
                f"Concluídos: {counts['completed']} | "
                f"Ignorados: {counts['skipped']} | Falhas: {counts['failed']}"
            ),
        )
        self.events.put(("manager_stopped", elapsed, counts))

    def add_overall_task(self, description: str, num_tasks: int) -> None:
        self.title = description
        self.total = num_tasks
        self.events.put(("plan", description, num_tasks))

    def add_task(self, current_task: int = 0, total: int = 100) -> int:
        del current_task, total
        with self._lock:
            self._task_id += 1
            return self._task_id

    def update_task(
        self,
        task_id: int,
        completed: int | float | None = None,
        advance: int = 0,
        *,
        visible: bool = True,
    ) -> None:
        del task_id, completed, advance, visible

    def update_log(self, *, event: str, details: str, verbose_only: bool = False) -> None:
        with self._lock:
            recorded = self.logger.log(
                event,
                details,
                disable_ui=False,
                verbose_only=verbose_only,
            )
        if recorded:
            stamp = datetime.now().strftime("%H:%M:%S")
            safe_details = redact_sensitive_urls(details)
            self.events.put(("log", stamp, event, safe_details))
            if event in FAILURE_EVENTS:
                self.events.put(("failure", stamp, event, safe_details))

    def update_summary(self, task_result: TaskResult, reason=None) -> None:  # noqa: ANN001
        with self._lock:
            self.summary.update_result(task_result, reason)
            counts = self._counts_unlocked()
        self.events.put(("progress", counts, self.total))

    def _counts(self) -> dict[str, int]:
        with self._lock:
            return self._counts_unlocked()

    def reset_summary(self) -> None:
        """The chosen download gets its own counters, not the scan's filters."""
        with self._lock:
            self.summary = SummaryManager()
            self.total = 0
        self.events.put(("progress", self._counts(), 0))

    def _counts_unlocked(self) -> dict[str, int]:
        return {
            "completed": self.summary.get_result_count(TaskResult.COMPLETED),
            "skipped": self.summary.get_result_count(TaskResult.SKIPPED),
            "failed": self.summary.get_result_count(TaskResult.FAILED),
        }


class BuidlApp(ctk.CTk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — Media Downloader (HTTPS corrigido)")
        self.geometry("1120x760")
        self.minsize(980, 680)
        self.configure(fg_color="#0D0F17")

        self.events: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.running = False
        self.started_at = 0.0
        self.total_files = 0
        self.last_counts = {"completed": 0, "skipped": 0, "failed": 0}
        self.failures: list[tuple[str, str, str]] = []
        self.current_log_file = default_download_directory() / "buidl-gui.log"
        self.preview_window: PreviewWindow | None = None
        self.selection_queue: queue.Queue = queue.Queue()
        self.preview_phase = False
        self.preview_session: PreviewSession | None = None
        self.active_preview_key: tuple = ()
        self.recovery = RecoveryStore()
        self._resume_document = None
        self.recovery_error = None

        self._build_layout()
        self._load_defaults()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)
        self.after(300, self._offer_recovery)

    def _offer_recovery(self):
        if self.running:
            return
        pending = self.recovery.pending()
        if not pending:
            return
        document = pending[0]
        answer = messagebox.askyesnocancel(
            "Continuar download interrompido?",
            "Há um download que não terminou. Continuar da fila salva, preservando os arquivos já baixados?\n\n"
            "Sim: continuar. Não: dispensar este aviso. Cancelar: decidir depois.", parent=self,
        )
        if answer is None:
            return
        if not answer:
            self.recovery.path = Path(document["_path"])
            self.recovery.complete()
            return
        try:
            options = document["options"]
            values = {"url_entry": document["url"], "cookies_entry": document.get("cookies_file") or "",
                      "output_entry": recovery_output_directory(document),
                      "workers_entry": options["workers"], "connections_entry": options["connections"],
                      "retries_entry": options["max_retries"], "pages_entry": options["max_pages"],
                      "delay_entry": options["delay"], "ignore_entry": ",".join(options["ignore"]),
                      "include_entry": ",".join(options["include"]),
                      "rate_entry": options["rate_limit"] / (1024 * 1024) if options.get("rate_limit") else ""}
            for name, value in values.items():
                self._replace_entry(getattr(self, name), str(value))
            for name in ("all_pages", "dry_run", "separate_posts", "flat"):
                getattr(self, name).set(options.get(name, False))
            RecoveryStore.restore_prepared(document)
            self._resume_document = document
            preview = document.get("phase") == "selection" or (not document.get("prepared") and document.get("preview"))
            self._start_download(preview=preview)
        except (OSError, ValueError, KeyError, TypeError) as error:
            messagebox.showerror("Não foi possível restaurar a fila", str(error), parent=self)

    def _save_recovery(self, method, *args, **kwargs):
        try:
            getattr(self.recovery, method)(*args, **kwargs)
            return True
        except (OSError, ValueError) as error:
            if self.recovery_error is None:
                self.recovery_error = str(error)
                self.events.put(("log", datetime.now().strftime("%H:%M:%S"), "Aviso de retomada",
                                 "Não foi possível gravar a fila de recuperação. Verifique espaço e permissões da pasta de dados."))
            return False

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=228, corner_radius=0, fg_color="#11141E")
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(8, weight=1)

        brand = ctk.CTkLabel(
            sidebar,
            text="BUIDL",
            font=ctk.CTkFont("Segoe UI", 30, "bold"),
            text_color="#FFFFFF",
        )
        brand.grid(row=0, column=0, padx=28, pady=(30, 0), sticky="w")
        ctk.CTkLabel(
            sidebar,
            text="MEDIA DOWNLOADER",
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
            text_color=ACCENT_HOVER,
        ).grid(row=1, column=0, padx=30, pady=(0, 34), sticky="w")

        self._sidebar_item(sidebar, 2, "●", "Novo download", active=True)
        self._sidebar_item(sidebar, 3, "↻", "Retomada automática")
        self._sidebar_item(sidebar, 4, "✓", "Verificação de arquivos")

        status_card = ctk.CTkFrame(sidebar, fg_color=SURFACE_ALT, corner_radius=14)
        status_card.grid(row=6, column=0, padx=18, pady=28, sticky="ew")
        ctk.CTkLabel(
            status_card,
            text="STATUS",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=16, pady=(14, 3))
        self.status_label = ctk.CTkLabel(
            status_card,
            text="●  Pronto",
            text_color=SUCCESS,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        self.status_label.pack(anchor="w", padx=16, pady=(0, 14))

        self.theme_switch = ctk.CTkSwitch(
            sidebar,
            text="Modo claro",
            command=self._toggle_theme,
            progress_color=ACCENT,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.theme_switch.grid(row=9, column=0, padx=26, pady=(0, 16), sticky="w")
        ctk.CTkLabel(
            sidebar,
            text=f"v{__version__} · HTTPS corrigido",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 10),
        ).grid(row=10, column=0, padx=28, pady=(0, 24), sticky="w")

        main = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        main.grid(row=0, column=1, padx=30, pady=20, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            main,
            text="Baixar uma thread",
            font=ctk.CTkFont("Segoe UI", 26, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            main,
            text="Cole a URL, ajuste as opções e deixe o BUIDL cuidar do restante.",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 13),
        ).grid(row=1, column=0, pady=(2, 12), sticky="w")

        tabs = ctk.CTkTabview(
            main,
            height=252,
            fg_color=SURFACE,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            corner_radius=16,
        )
        tabs.grid(row=2, column=0, sticky="ew")
        self.settings_tabs = tabs
        basic = tabs.add("Principal")
        advanced = tabs.add("Avançado")
        self._build_basic_tab(basic)
        self._build_advanced_tab(advanced)

        progress_card = ctk.CTkFrame(main, fg_color=SURFACE, corner_radius=16)
        progress_card.grid(row=3, column=0, pady=(12, 8), sticky="ew")
        progress_card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(progress_card, fg_color="transparent")
        header.grid(row=0, column=0, padx=18, pady=(14, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.progress_title = ctk.CTkLabel(
            header,
            text="Aguardando um download",
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
        )
        self.progress_title.grid(row=0, column=0, sticky="w")
        self.progress_text = ctk.CTkLabel(
            header,
            text="0 / 0",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.progress_text.grid(row=0, column=1, sticky="e")
        self.progress = ctk.CTkProgressBar(
            progress_card,
            height=10,
            corner_radius=10,
            progress_color=ACCENT,
            fg_color="#292D3E",
        )
        self.progress.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="ew")
        self.progress.set(0)

        body = ctk.CTkFrame(main, fg_color="transparent")
        body.grid(row=4, column=0, sticky="nsew")
        body.grid_columnconfigure(0, minsize=205)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_metrics(body)
        self._build_log(body)

        actions = ctk.CTkFrame(main, fg_color="transparent")
        actions.grid(row=5, column=0, pady=(14, 0), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        self.open_button = ctk.CTkButton(
            actions,
            text="Abrir pasta",
            width=95,
            fg_color=SURFACE_ALT,
            hover_color="#292E43",
            command=self._open_downloads,
        )
        self.open_button.grid(row=0, column=0, sticky="w")
        self.preview_button = ctk.CTkButton(
            actions, text="Visualizar e escolher", width=170, height=40,
            fg_color="#353052", hover_color="#484060",
            command=lambda: self._start_download(preview=True),
        )
        self.preview_button.grid(row=0, column=1, padx=(8, 0))
        self.pause_button = ctk.CTkButton(
            actions,
            text="Pausar",
            width=75,
            state="disabled",
            fg_color=SURFACE_ALT,
            hover_color="#292E43",
            command=self._toggle_pause,
        )
        self.pause_button.grid(row=0, column=2, padx=8)
        self.cancel_button = ctk.CTkButton(
            actions,
            text="Cancelar",
            width=80,
            state="disabled",
            fg_color="#3A2027",
            hover_color="#552C36",
            text_color="#FF9BAA",
            command=self._cancel,
        )
        self.cancel_button.grid(row=0, column=3, padx=(0, 8))
        self.start_button = ctk.CTkButton(
            actions,
            text="Baixar tudo",
            width=125,
            height=40,
            corner_radius=11,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._start_download,
        )
        self.start_button.grid(row=0, column=4)

    def _sidebar_item(self, parent, row: int, icon: str, text: str, *, active=False) -> None:  # noqa: ANN001
        color = ACCENT if active else "transparent"
        frame = ctk.CTkFrame(parent, fg_color=color, corner_radius=10)
        frame.grid(row=row, column=0, padx=18, pady=4, sticky="ew")
        ctk.CTkLabel(
            frame,
            text=f"{icon}   {text}",
            font=ctk.CTkFont("Segoe UI", 12, "bold" if active else "normal"),
            text_color="#FFFFFF" if active else TEXT_MUTED,
        ).pack(anchor="w", padx=14, pady=10)

    def _build_basic_tab(self, parent) -> None:  # noqa: ANN001
        parent.grid_columnconfigure(0, weight=1)
        self.url_entry = self._labeled_entry(parent, 0, "URL DA THREAD", "https://...")

        paths = ctk.CTkFrame(parent, fg_color="transparent")
        paths.grid(row=2, column=0, padx=4, pady=(4, 0), sticky="ew")
        paths.grid_columnconfigure((0, 1), weight=1)
        self.cookies_entry = self._path_entry(
            paths,
            row=0,
            column=0,
            columnspan=1,
            label="COOKIES (OPCIONAL)",
            value="",
            command=self._choose_cookies,
        )
        self.output_entry = self._path_entry(
            paths,
            row=0,
            column=1,
            columnspan=1,
            label="SALVAR EM (PASTA EXATA)",
            value=str(default_download_directory()),
            command=self._choose_output,
        )

        options = ctk.CTkFrame(parent, fg_color="transparent")
        options.grid(row=3, column=0, padx=12, pady=(10, 2), sticky="ew")
        self.all_pages = ctk.BooleanVar(value=True)
        self.separate_posts = ctk.BooleanVar(value=False)
        self.flat = ctk.BooleanVar(value=False)
        self.dry_run = ctk.BooleanVar(value=False)
        for column, (label, variable) in enumerate(
            (
                ("Todas as páginas", self.all_pages),
                ("Separar posts", self.separate_posts),
                ("Pasta única", self.flat),
                ("Somente simular", self.dry_run),
            )
        ):
            ctk.CTkCheckBox(
                options,
                text=label,
                variable=variable,
                checkbox_width=18,
                checkbox_height=18,
                fg_color=ACCENT,
                hover_color=ACCENT_HOVER,
                font=ctk.CTkFont("Segoe UI", 11),
            ).grid(row=0, column=column, padx=(0, 22), sticky="w")

    def _build_advanced_tab(self, parent) -> None:  # noqa: ANN001
        for column in range(4):
            parent.grid_columnconfigure(column, weight=1)
        self.workers_entry = self._small_entry(
            parent, 0, 0, "WORKERS", str(MAX_WORKERS)
        )
        self.connections_entry = self._small_entry(
            parent, 0, 1, "CONEXÕES/ARQUIVO", "1"
        )
        self.retries_entry = self._small_entry(
            parent, 0, 2, "TENTATIVAS", str(MAX_RETRIES)
        )
        self.pages_entry = self._small_entry(parent, 0, 3, "LIMITE DE PÁGINAS", "1")
        self.delay_entry = self._small_entry(
            parent, 1, 0, "INTERVALO (S)", str(INTER_FILE_DELAY)
        )
        self.rate_entry = self._small_entry(
            parent, 1, 1, "LIMITE MB/S", "", columnspan=1
        )
        self.include_entry = self._small_entry(
            parent,
            1,
            2,
            "INCLUIR (VÍRGULAS)",
            "",
            columnspan=1,
        )
        self.ignore_entry = self._small_entry(
            parent,
            1,
            3,
            "IGNORAR (VÍRGULAS)",
            "",
            columnspan=1,
        )

    def _labeled_entry(self, parent, row: int, label: str, placeholder: str):  # noqa: ANN001, ANN202
        ctk.CTkLabel(
            parent,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
        ).grid(row=row, column=0, padx=12, pady=(8, 0), sticky="w")
        entry = ctk.CTkEntry(
            parent,
            height=38,
            placeholder_text=placeholder,
            fg_color=SURFACE_ALT,
            border_width=0,
        )
        entry.grid(row=row + 1, column=0, padx=12, pady=(4, 0), sticky="ew")
        return entry

    def _small_entry(
        self,
        parent,  # noqa: ANN001
        row: int,
        column: int,
        label: str,
        value: str,
        *,
        columnspan: int = 1,
    ):  # noqa: ANN202
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            padx=8,
            pady=(3, 0),
            sticky="ew",
        )
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        entry = ctk.CTkEntry(frame, height=32, fg_color=SURFACE_ALT, border_width=0)
        entry.grid(row=1, column=0, pady=(2, 0), sticky="ew")
        if value:
            entry.insert(0, value)
        return entry

    def _path_entry(
        self,
        parent,  # noqa: ANN001
        *,
        row: int,
        column: int,
        columnspan: int,
        label: str,
        value: str,
        command,
    ):  # noqa: ANN001, ANN202
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            padx=8,
            pady=(3, 0),
            sticky="ew",
        )
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text=label,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        entry = ctk.CTkEntry(frame, height=32, fg_color=SURFACE_ALT, border_width=0)
        entry.grid(row=1, column=0, pady=(2, 0), sticky="ew")
        entry.insert(0, value)
        ctk.CTkButton(
            frame,
            text="Procurar",
            width=76,
            height=32,
            fg_color="#292E43",
            hover_color="#353B55",
            command=command,
        ).grid(row=1, column=1, padx=(8, 0), pady=(2, 0))
        return entry

    def _build_metrics(self, parent) -> None:  # noqa: ANN001
        metrics = ctk.CTkFrame(parent, width=205, fg_color=SURFACE, corner_radius=16)
        metrics.grid(row=0, column=0, padx=(0, 12), sticky="nsew")
        metrics.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            metrics,
            text="RESUMO",
            height=18,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
        ).pack(anchor="w", padx=18, pady=(8, 2))
        self.completed_value = self._metric(metrics, "Concluídos", SUCCESS)
        self.skipped_value = self._metric(metrics, "Ignorados", WARNING)
        self.failed_value = self._metric(metrics, "Falhas", DANGER)
        self.elapsed_value = self._metric(metrics, "Tempo", ACCENT_HOVER, initial="00:00")
        self.failure_button = ctk.CTkButton(
            metrics,
            text="Ver falhas",
            height=28,
            state="disabled",
            fg_color="#3A2027",
            hover_color="#552C36",
            text_color="#FF9BAA",
            font=ctk.CTkFont("Segoe UI", 11, "bold"),
            command=self._show_failures,
        )
        self.failure_button.pack(fill="x", padx=18, pady=(4, 6))

    def _metric(self, parent, label: str, color: str, initial="0"):  # noqa: ANN001, ANN202
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=1)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            row,
            text=label,
            text_color=TEXT_MUTED,
            anchor="w",
            height=20,
            font=ctk.CTkFont("Segoe UI", 11),
        ).grid(row=0, column=0, sticky="ew")
        value = ctk.CTkLabel(
            row,
            text=initial,
            height=20,
            width=62,
            anchor="e",
            text_color=color,
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        value.grid(row=0, column=1, padx=(12, 0), sticky="e")
        return value

    def _build_log(self, parent) -> None:  # noqa: ANN001
        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=16)
        card.grid(row=0, column=1, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            card,
            text="ATIVIDADE",
            text_color=TEXT_MUTED,
            font=ctk.CTkFont("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, padx=16, pady=(14, 6), sticky="w")
        self.log_box = ctk.CTkTextbox(
            card,
            height=150,
            fg_color="#10131C",
            border_width=0,
            corner_radius=10,
            text_color="#C9CEDA",
            font=ctk.CTkFont("Cascadia Mono", 10),
            wrap="word",
        )
        self.log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self.log_box.configure(state="disabled")

    def _load_defaults(self) -> None:
        cookie = Path.cwd() / "cookies.txt"
        if cookie.is_file():
            self.cookies_entry.insert(0, str(cookie))

    def _toggle_theme(self) -> None:
        ctk.set_appearance_mode("light" if self.theme_switch.get() else "dark")

    def _choose_cookies(self) -> None:
        selected = filedialog.askopenfilename(
            title="Selecione cookies.txt",
            filetypes=(("Cookies Netscape", "*.txt"), ("Todos os arquivos", "*.*")),
        )
        if selected:
            self._replace_entry(self.cookies_entry, selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="Selecione a pasta base")
        if selected:
            self._replace_entry(self.output_entry, selected)

    @staticmethod
    def _replace_entry(entry, value: str) -> None:  # noqa: ANN001
        entry.delete(0, "end")
        entry.insert(0, value)

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    def _start_download(self, *, preview: bool = False) -> None:
        if self.running:
            return
        try:
            url = self.url_entry.get().strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Informe uma URL HTTP(S) válida.")

            cookie_value = self.cookies_entry.get().strip()
            if cookie_value and not Path(cookie_value).is_file():
                raise ValueError("O arquivo de cookies não existe.")

            output_value = self.output_entry.get().strip()
            if not output_value:
                raise ValueError("Selecione uma pasta base.")
            validate_output_directory(output_value)

            workers = self._positive_int(self.workers_entry.get(), "Workers")
            connections = self._positive_int(self.connections_entry.get(), "Conexões")
            retries = self._nonnegative_int(self.retries_entry.get(), "Tentativas")
            pages = self._positive_int(self.pages_entry.get(), "Limite de páginas")
            delay = self._nonnegative_float(self.delay_entry.get(), "Intervalo")
            rate_text = self.rate_entry.get().strip()
            rate_limit = self._positive_float(rate_text, "Limite MB/s") if rate_text else None
        except ValueError as error:
            messagebox.showerror("Verifique os campos", str(error), parent=self)
            return

        self.cancel_event.clear()
        self.selection_queue = queue.Queue()
        self.preview_phase = preview
        self.pause_event.set()
        self.running = True
        self.started_at = time.monotonic()
        self.total_files = 0
        self.last_counts = {"completed": 0, "skipped": 0, "failed": 0}
        self.failures.clear()
        self._set_running_ui(True)
        self._clear_log()
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.progress_title.configure(text="Analisando páginas e resolvendo links…")
        self.progress_text.configure(text="Preparando")
        self.status_label.configure(text="●  Em execução", text_color=ACCENT_HOVER)

        options = ThreadOptions(
            download_path=output_value,
            max_pages=pages,
            all_pages=self.all_pages.get(),
            ignore=self._csv(self.ignore_entry.get()),
            include=self._csv(self.include_entry.get()),
            workers=workers,
            max_retries=retries,
            delay=delay,
            connections=connections,
            rate_limit=rate_limit * 1024 * 1024 if rate_limit else None,
            dry_run=self.dry_run.get(),
            separate_posts=self.separate_posts.get(),
            flat=self.flat.get(),
            cancel_event=self.cancel_event,
            pause_event=self.pause_event,
        )
        resume_document = self._resume_document
        self._resume_document = None
        self.recovery_error = None
        if not options.dry_run:
            self._save_recovery("begin", url, cookie_value or None, options, preview, resume=resume_document)
        log_file = Path(output_value) / "buidl-gui.log"
        cached = None
        if preview:
            try:
                self.active_preview_key = preview_fingerprint(url, cookie_value or None, options)
            except OSError as error:
                self._finish_download(True, False)
                messagebox.showerror("Não foi possível ler as opções", str(error), parent=self)
                return
            if self.preview_session is not None:
                if self.preview_session.fingerprint == self.active_preview_key:
                    cached = self.preview_session.prepared
                    self.failures.extend(self.preview_session.warnings)
                    self.progress_title.configure(text="Reabrindo galeria já carregada…")
                else:
                    self.preview_session = None
        self.current_log_file = log_file
        self.worker_thread = threading.Thread(
            target=self._download_worker,
            args=(url, cookie_value or None, options, log_file, preview, self.selection_queue, cached, resume_document),
            daemon=True,
            name="buidl-download",
        )
        self.worker_thread.start()

    def _download_worker(
        self,
        url: str,
        cookies_file: str | None,
        options: ThreadOptions,
        log_file: Path,
        preview: bool = False,
        decisions: queue.Queue | None = None,
        cached=None,
        resume_document=None,
    ) -> None:
        manager = GuiManager(self.events, log_file)
        client = None
        failed = True
        try:
            manager.start()
            client = HttpClient(cookies_file=cookies_file)
            downloader = ThreadDownloader(client, manager, options)
            prepared = RecoveryStore.restore_prepared(resume_document) if resume_document else cached
            if prepared is None:
                prepared = downloader.prepare_thread(url)
            if prepared is not None and not options.dry_run:
                self._save_recovery("save_prepared", prepared, preview)
            if not preview:
                selection = resume_document.get("selection") if resume_document else None
                failed = True if prepared is None else downloader.download_prepared(prepared, set(selection) if selection is not None else None)
            else:
                if cached is not None:
                    manager.update_log(event="Galeria reutilizada", details="Lista mantida nesta sessão; sem nova busca da thread.")
                while prepared is not None and prepared.plan and not self.cancel_event.is_set():
                    self.events.put(("preview_ready", prepared, cookies_file))
                    # Keep the same client/options/resolved queue until the
                    # user chooses. No download or signing occurs while waiting.
                    while not self.cancel_event.is_set():
                        try:
                            keys = decisions.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        break
                    if self.cancel_event.is_set():
                        break
                    if keys is PreviewCommand.REFRESH:
                        manager.reset_summary()
                        prepared = downloader.prepare_thread(url)
                        continue
                    manager.reset_summary()
                    failed = downloader.download_prepared(prepared, keys)
                    break
                if prepared is not None and not prepared.plan and not self.cancel_event.is_set():
                    self.events.put(("fatal", "Nenhum arquivo disponível para visualizar. Confira os filtros e o log."))
        except Exception as error:  # noqa: BLE001
            manager.update_log(event="Erro inesperado", details=str(error))
            self.events.put(("fatal", str(error)))
        finally:
            if client is not None:
                client.close()
            manager.stop()
            if not failed and not self.cancel_event.is_set() and not options.dry_run:
                self._save_recovery("complete")
            self.events.put(("done", failed, self.cancel_event.is_set()))

    def _poll_events(self) -> None:
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    _, stamp, name, details = event
                    self._append_log(stamp, name, details)
                elif kind == "failure":
                    _, stamp, name, details = event
                    self.failures.append((stamp, name, details))
                elif kind == "plan":
                    _, title, total = event
                    self.total_files = total
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    self.progress_title.configure(text=title)
                    self.progress_text.configure(text=f"0 / {total}")
                elif kind == "progress":
                    _, counts, total = event
                    self._update_counts(counts, total)
                elif kind == "manager_stopped":
                    _, elapsed, counts = event
                    self._update_counts(counts, self.total_files)
                    self.elapsed_value.configure(text=self._format_elapsed(elapsed))
                elif kind == "fatal":
                    _, details = event
                    messagebox.showerror("Falha no download", details, parent=self)
                elif kind == "done":
                    _, failed, cancelled = event
                    self._finish_download(failed, cancelled)
                elif kind == "preview_ready":
                    _, prepared, cookies_file = event
                    if self.cancel_event.is_set():
                        continue
                    if self.preview_session is None or self.preview_session.prepared is not prepared:
                        self.preview_session = PreviewSession(
                            prepared, self.active_preview_key, warnings=list(self.failures),
                        )
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    self.progress_title.configure(text="Prévia pronta — escolha os arquivos na galeria")
                    self.progress_text.configure(text=f"{len(prepared.plan)} encontrados")
                    self.status_label.configure(text="●  Aguardando seleção", text_color=ACCENT_HOVER)
                    self.pause_button.configure(state="disabled")
                    self.preview_window = PreviewWindow(
                        self, prepared, cookies_file, self._confirm_selection, self._cancel_preview,
                        session=self.preview_session, on_refresh=self._refresh_preview,
                    )
        except queue.Empty:
            pass

        if self.running:
            self.elapsed_value.configure(
                text=self._format_elapsed(time.monotonic() - self.started_at)
            )
        self.after(80, self._poll_events)

    def _confirm_selection(self, keys: set[str] | None) -> None:
        self.preview_window = None
        self.preview_phase = False
        self.pause_event.set()
        self.pause_button.configure(state="normal", text="Pausar")
        self.status_label.configure(text="●  Baixando seleção", text_color=ACCENT_HOVER)
        self.progress_title.configure(text="Iniciando os arquivos escolhidos…")
        if not self.dry_run.get():
            self._save_recovery("select", keys)
        self.selection_queue.put(keys)

    def _cancel_preview(self) -> None:
        self.preview_window = None
        self._cancel()

    def _refresh_preview(self) -> None:
        """Refresh explicitly while keeping the existing worker/client alive."""
        self.preview_window = None
        self.preview_session = None
        self.failures.clear()
        self.pause_event.set()
        self.pause_button.configure(state="normal", text="Pausar")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.progress_title.configure(text="Atualizando a thread — buscando páginas e links…")
        self.progress_text.configure(text="Atualizando")
        self.status_label.configure(text="●  Atualizando thread", text_color=ACCENT_HOVER)
        self.selection_queue.put(PreviewCommand.REFRESH)

    def _update_counts(self, counts: dict[str, int], total: int) -> None:
        self.last_counts = counts
        done = sum(counts.values())
        self.completed_value.configure(text=str(counts["completed"]))
        self.skipped_value.configure(text=str(counts["skipped"]))
        self.failed_value.configure(text=str(counts["failed"]))
        failures = max(counts["failed"], len(self.failures))
        self.failure_button.configure(
            text=f"Ver falhas ({failures})" if failures else "Ver falhas",
            state="normal" if failures else "disabled",
        )
        if total > 0:
            self.progress.set(min(done / total, 1.0))
            self.progress_text.configure(text=f"{done} / {total}")

    def _append_log(self, stamp: str, event: str, details: str) -> None:
        clean = details.replace("\n", " · ")
        line = f"{stamp}  {event:<18} {clean}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._update_counts({"completed": 0, "skipped": 0, "failed": 0}, 0)
        self.elapsed_value.configure(text="00:00")

    def _show_failures(self) -> None:
        if not self.failures:
            messagebox.showinfo(
                "Detalhes das falhas",
                "Não há detalhes carregados nesta execução. Consulte o arquivo de log.",
                parent=self,
            )
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Falhas do download ({len(self.failures)})")
        dialog.geometry("780x470")
        dialog.minsize(640, 380)
        dialog.configure(fg_color="#0D0F17")
        dialog.transient(self)
        dialog.grab_set()
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            dialog,
            text=f"{len(self.failures)} arquivo(s) não foram baixados",
            font=ctk.CTkFont("Segoe UI", 20, "bold"),
            text_color="#FFFFFF",
        ).grid(row=0, column=0, padx=24, pady=(22, 2), sticky="w")
        ctk.CTkLabel(
            dialog,
            text="O motivo e a URL de cada falha estão listados abaixo.",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=TEXT_MUTED,
        ).grid(row=1, column=0, padx=24, pady=(0, 12), sticky="w")

        details_box = ctk.CTkTextbox(
            dialog,
            fg_color="#10131C",
            border_width=0,
            corner_radius=12,
            text_color="#E2E5EC",
            font=ctk.CTkFont("Cascadia Mono", 11),
            wrap="word",
        )
        details_box.grid(row=2, column=0, padx=24, sticky="nsew")
        report = self._failure_report()
        details_box.insert("1.0", report)
        details_box.configure(state="disabled")

        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.grid(row=3, column=0, padx=24, pady=18, sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            actions,
            text="Abrir log completo",
            width=140,
            fg_color=SURFACE_ALT,
            hover_color="#292E43",
            command=self._open_log,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            actions,
            text="Copiar detalhes",
            width=130,
            fg_color=SURFACE_ALT,
            hover_color="#292E43",
            command=lambda: self._copy_failure_report(report),
        ).grid(row=0, column=1, padx=8)
        ctk.CTkButton(
            actions,
            text="Fechar",
            width=100,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=dialog.destroy,
        ).grid(row=0, column=2)

    def _failure_report(self) -> str:
        blocks = []
        for index, (stamp, event, details) in enumerate(self.failures, start=1):
            blocks.append(f"#{index}  {stamp}  {event}\n{details}")
        return "\n\n".join(blocks)

    def _copy_failure_report(self, report: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(report)
        self.update()

    def _open_log(self) -> None:
        if not self.current_log_file.exists():
            messagebox.showinfo(
                "Log ainda não criado",
                f"O arquivo ainda não existe:\n{self.current_log_file}",
                parent=self,
            )
            return
        try:
            os.startfile(self.current_log_file)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("Não foi possível abrir o log", str(error), parent=self)

    def _toggle_pause(self) -> None:
        if not self.running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text="Continuar")
            self.status_label.configure(text="●  Pausado", text_color=WARNING)
            self._append_log(datetime.now().strftime("%H:%M:%S"), "Pausado", "Novos arquivos aguardarão.")
        else:
            self.pause_event.set()
            self.pause_button.configure(text="Pausar")
            self.status_label.configure(text="●  Em execução", text_color=ACCENT_HOVER)
            self._append_log(datetime.now().strftime("%H:%M:%S"), "Retomado", "Download continuado.")

    def _cancel(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.pause_event.set()
        self.cancel_button.configure(state="disabled", text="Cancelando…")
        self.status_label.configure(text="●  Cancelando", text_color=WARNING)

    def _finish_download(self, failed: bool, cancelled: bool) -> None:
        self.running = False
        self.progress.stop()
        self._set_running_ui(False)
        if not self.total_files:
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self.progress_text.configure(text="Não iniciado")
        if cancelled:
            self.status_label.configure(text="●  Cancelado", text_color=WARNING)
            self.progress_title.configure(text=(
                "Prévia cancelada — nenhum arquivo foi baixado" if self.preview_phase
                else "Download cancelado — partes mantidas para retomada"
            ))
        elif failed or self.last_counts["failed"] or self.failures:
            self.status_label.configure(text="●  Finalizado com falhas", text_color=DANGER)
            count = max(self.last_counts["failed"], len(self.failures))
            self.progress_title.configure(
                text=f"Finalizado — {count} falha(s) · clique em Ver falhas"
            )
        else:
            self.status_label.configure(text="●  Concluído", text_color=SUCCESS)
            self.progress_title.configure(text="Download concluído com sucesso")
            if self.total_files:
                self.progress.set(1)

    def _set_running_ui(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.preview_button.configure(state="disabled" if running else "normal")
        self._set_settings_state(self.settings_tabs, "disabled" if running else "normal")
        self.pause_button.configure(state="normal" if running else "disabled", text="Pausar")
        self.cancel_button.configure(
            state="normal" if running else "disabled",
            text="Cancelar",
        )

    def _set_settings_state(self, widget, state: str) -> None:
        if isinstance(widget, (ctk.CTkEntry, ctk.CTkCheckBox, ctk.CTkButton)):
            widget.configure(state=state)
            return
        for child in widget.winfo_children():
            self._set_settings_state(child, state)

    def _open_downloads(self) -> None:
        base = Path(self.output_entry.get().strip() or default_download_directory())
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(base)  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror("Não foi possível abrir a pasta", str(error), parent=self)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Download em andamento",
                "Cancelar o download e fechar? As partes serão preservadas para retomada.",
                parent=self,
            ):
                return
            self.cancel_event.set()
            self.pause_event.set()
        if self.preview_window is not None:
            self.preview_window.close()
        self.destroy()

    @staticmethod
    def _positive_int(value: str, label: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise ValueError(f"{label} deve ser um número inteiro.") from error
        if parsed < 1:
            raise ValueError(f"{label} deve ser pelo menos 1.")
        return parsed

    @staticmethod
    def _nonnegative_int(value: str, label: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise ValueError(f"{label} deve ser um número inteiro.") from error
        if parsed < 0:
            raise ValueError(f"{label} não pode ser negativo.")
        return parsed

    @staticmethod
    def _nonnegative_float(value: str, label: str) -> float:
        try:
            parsed = float(value.replace(",", "."))
        except ValueError as error:
            raise ValueError(f"{label} deve ser um número.") from error
        if parsed < 0:
            raise ValueError(f"{label} não pode ser negativo.")
        return parsed

    @classmethod
    def _positive_float(cls, value: str, label: str) -> float:
        parsed = cls._nonnegative_float(value, label)
        if parsed <= 0:
            raise ValueError(f"{label} deve ser maior que zero.")
        return parsed

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = int(seconds)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{secs:02}" if hours else f"{minutes:02}:{secs:02}"


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = BuidlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
