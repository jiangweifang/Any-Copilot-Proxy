from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame,
                              QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                              QProgressBar, QPushButton, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

from .config import InstanceLock, ListenerSettings, UserStore
from .i18n import TEXT, localize_message, tr
from .platforms import clean_installation, install_startup, remove_startup, startup_enabled
from .runtime import CONTROL_CONNECTION_ERRORS, control_request, safe_error, serve

STYLE = """
QWidget { background:#10131b; color:#e8eaf4; font-size:14px; }
QMainWindow, QDialog { background:#10131b; }
QFrame#card { background:#191e2a; border:1px solid #2b3346; border-radius:16px; }
QFrame#card QLabel { background:transparent; }
QLabel#heading { font-size:29px; font-weight:700; }
QLabel#muted { color:#9da8c2; }
QLabel#badge { color:#b9a4ff; background:#29233e; border-radius:9px; padding:7px 12px; }
QPushButton { background:#282f42; border:1px solid #3a4560; border-radius:9px; padding:10px 16px; }
QPushButton:hover { background:#35415b; }
QPushButton:disabled { color:#65708b; background:#1d2331; }
QPushButton#primary { background:#7862de; border-color:#9583ef; font-weight:600; }
QPushButton#danger { color:#f3a1af; }
QLineEdit,QComboBox,QSpinBox { background:#111622; border:1px solid #3b465e; border-radius:8px; padding:9px; }
QLineEdit:focus,QComboBox:focus,QSpinBox:focus { border-color:#a493f5; }
QMenu { background:#191e2a; border:1px solid #3b465e; }
QMenu::item { padding:9px 24px; }
QMenu::item:selected { background:#343054; }
QProgressBar { background:#111622; border:1px solid #3b465e; border-radius:7px; text-align:center; min-height:18px; color:#e8eaf4; font-size:12px; }
QProgressBar::chunk { background:#7862de; border-radius:6px; }
"""


def label(text="", name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#7862de"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    painter.setPen(QColor("white"))
    painter.setFont(QFont("Arial", 21, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "CP")
    painter.end()
    return QIcon(pixmap)


def confirm(parent, language, title, body, **values):
    box = QMessageBox(parent)
    box.setWindowTitle(tr(language, title))
    box.setText(tr(language, body, **values))
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.button(QMessageBox.StandardButton.Yes).setText(tr(language, "yes"))
    box.button(QMessageBox.StandardButton.No).setText(tr(language, "no"))
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def inform(parent, language, title, body, **values):
    box = QMessageBox(parent)
    box.setWindowTitle(tr(language, title))
    box.setText(tr(language, body, **values))
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.button(QMessageBox.StandardButton.Ok).setText(tr(language, "ok"))
    box.exec()


class JobSignals(QObject):
    result = Signal(str, object)
    error = Signal(str, str)


class Job(QRunnable):
    def __init__(self, key, work):
        super().__init__()
        self.key, self.work = key, work
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.result.emit(self.key, self.work())
        except Exception as error:
            self.signals.error.emit(self.key, safe_error(error))


class Dispatcher(QObject):
    result = Signal(str, object)
    error = Signal(str, str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.jobs = {}

    def work(self, key, operation):
        if key in self.jobs:
            return
        job = Job(key, operation)
        job.signals.result.connect(self._result)
        job.signals.error.connect(self._error)
        self.jobs[key] = job
        QThreadPool.globalInstance().start(job)

    def request(self, key, request):
        self.work(key, lambda: control_request(self.store, request))

    @Slot(str, object)
    def _result(self, key, result):
        self.jobs.pop(key, None)
        self.result.emit(key, result)

    @Slot(str, str)
    def _error(self, key, error):
        self.jobs.pop(key, None)
        self.error.emit(key, error)


class ProxyDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.language = parent.language if isinstance(parent, MainWindow) else store.read("ui", {}).get("language", "zh")
        self.raw_message = ""
        self.message_kind = "plain"
        self.setWindowTitle("Network settings - Any-Copilot-Proxy")
        self.resize(530, 500)
        self.dispatch = Dispatcher(store, self)
        self.dispatch.result.connect(self.completed)
        self.dispatch.error.connect(self.failed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(16)
        self.heading = label("", "heading")
        self.subtitle = label("", "muted")
        layout.addWidget(self.heading)
        layout.addWidget(self.subtitle)
        self.form_layout = QFormLayout()
        self.kind = QComboBox()
        for text, value in (("", "none"), ("HTTP", "http"), ("SOCKS5", "socks5")):
            self.kind.addItem(text, value)
        self.host = QLineEdit()
        self.host.setPlaceholderText("127.0.0.1 or proxy.example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(8080)
        self.username, self.password = QLineEdit(), QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.keep = QCheckBox("Keep saved password when this field is empty")
        for name, field in (("Protocol", self.kind), ("Server", self.host), ("Port", self.port), ("Username", self.username), ("Password", self.password)):
            self.form_layout.addRow(name, field)
        layout.addLayout(self.form_layout)
        layout.addWidget(self.keep)
        self.password_notice = label("", "muted")
        layout.addWidget(self.password_notice)
        self.message = label()
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.test = QPushButton("Test connection")
        self.save = QPushButton("Save and apply")
        self.save.setObjectName("primary")
        self.cancel = QPushButton("Cancel")
        for button in (self.test, self.save, self.cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.test.clicked.connect(lambda: self.submit("proxy-test"))
        self.save.clicked.connect(lambda: self.submit("proxy-set"))
        self.cancel.clicked.connect(self.reject)
        self.kind.currentTextChanged.connect(self.update_fields)
        if isinstance(parent, MainWindow):
            parent.language_changed.connect(self.retranslate)
        self.retranslate(self.language)
        self.update_fields()
        self.dispatch.request("proxy-get", {"command": "proxy-get"})

    def update_fields(self):
        enabled = self.kind.currentData() != "none"
        for field in (self.host, self.port, self.username, self.password, self.keep):
            field.setEnabled(enabled)

    def submit(self, command):
        if command == "proxy-set" and not confirm(self, self.language, "apply_title", "apply_body"):
            return
        request = {"command": command, "keepPassword": self.keep.isChecked() and not self.password.text(),
                   "proxy": {"type": self.kind.currentData(), "host": self.host.text(), "port": self.port.value(),
                             "username": self.username.text(), "password": self.password.text()}}
        self.test.setEnabled(False)
        self.save.setEnabled(False)
        self.cancel.setEnabled(False)
        self.message_kind = "working"
        self.render_message()
        self.dispatch.request(command, request)

    @Slot(str, object)
    def completed(self, command, result):
        if command == "proxy-get":
            self.kind.setCurrentIndex(self.kind.findData(result["type"]))
            self.host.setText(result.get("host", ""))
            self.port.setValue(result.get("port", 8080))
            self.username.setText(result.get("username", ""))
            self.keep.setChecked(result.get("hasPassword", False))
        elif command == "proxy-set":
            self.message_kind, self.raw_message = "saved", result.get("message", "")
            self.render_message()
            self.password.clear()
            self.dispatch.request("proxy-get", {"command": "proxy-get"})
        else:
            self.message_kind, self.raw_message = "plain", result["message"]
            self.render_message()
        for button in (self.test, self.save, self.cancel):
            button.setEnabled(True)

    @Slot(str, str)
    def failed(self, command, error):
        self.message_kind, self.raw_message = "plain", error
        self.render_message()
        for button in (self.test, self.save, self.cancel):
            button.setEnabled(True)


    def render_message(self):
        value = localize_message(self.language, self.raw_message)
        self.message.setText(tr(self.language, "working") if self.message_kind == "working" else
                             tr(self.language, "saved", message=value) if self.message_kind == "saved" else value)

    @Slot(str)
    def retranslate(self, language):
        self.language = language
        self.setWindowTitle(tr(language, "network") + " - Any-Copilot-Proxy")
        self.heading.setText(tr(language, "network"))
        self.subtitle.setText(tr(language, "proxy_subtitle"))
        self.kind.setItemText(0, tr(language, "direct"))
        self.host.setPlaceholderText(tr(language, "server_placeholder"))
        for key, widget in (("protocol", self.kind), ("server", self.host), ("port", self.port),
                            ("username", self.username), ("password", self.password)):
            self.form_layout.labelForField(widget).setText(tr(language, key))
        self.keep.setText(tr(language, "keep_password"))
        self.password_notice.setText(tr(language, "password_notice"))
        for key, button in (("test", self.test), ("save", self.save), ("cancel", self.cancel)):
            button.setText(tr(language, key))
        self.render_message()


class ListenerDialog(QDialog):
    applied = Signal(object)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.language = parent.language if isinstance(parent, MainWindow) else store.read("ui", {}).get("language", "zh")
        self.bridge_port = 4141
        self.busy = False
        self.loaded = False
        self.message_key, self.raw_message = "listener_loading", ""
        self.saved_url = ""
        self.resize(540, 370)
        self.dispatch = Dispatcher(store, self)
        self.dispatch.result.connect(self.completed)
        self.dispatch.error.connect(self.failed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(16)
        self.heading = label("", "heading")
        self.description = label("", "muted")
        layout.addWidget(self.heading)
        layout.addWidget(self.description)
        self.form_layout = QFormLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("127.0.0.1 / 0.0.0.0")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.form_layout.addRow("host", self.host)
        self.form_layout.addRow("port", self.port)
        layout.addLayout(self.form_layout)
        self.compatibility = label("", "muted")
        self.message = label()
        layout.addWidget(self.compatibility)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.save = QPushButton()
        self.save.setObjectName("primary")
        self.save.setEnabled(False)
        self.cancel = QPushButton()
        buttons.addWidget(self.save)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)
        self.save.clicked.connect(self.submit)
        self.cancel.clicked.connect(self.reject)
        if isinstance(parent, MainWindow):
            parent.language_changed.connect(self.retranslate)
        self.retranslate(self.language)
        self.dispatch.request("listener-get", {"command": "listener-get"})

    def reject(self):
        if not self.busy:
            super().reject()

    def submit(self):
        if self.busy or not self.loaded:
            return
        try:
            settings = ListenerSettings.from_data({"host": self.host.text().strip(), "port": self.port.value(), "bridgePort": self.bridge_port})
        except ValueError as error:
            self.failed("listener-set", str(error))
            return
        if not confirm(self, self.language, "listener_apply_title", "listener_apply_body", host=settings.host, port=settings.port):
            return
        self.busy = True
        self.save.setEnabled(False)
        self.cancel.setEnabled(False)
        self.host.setEnabled(False)
        self.port.setEnabled(False)
        self.message_key = "working"
        self.render_message()
        self.dispatch.request("listener-set", {"command": "listener-set", "listener": {"host": settings.host, "port": settings.port}})

    @Slot(str, object)
    def completed(self, command, result):
        if command == "listener-get":
            self.host.setText(result["host"])
            self.port.setValue(result["port"])
            self.bridge_port = result["bridgePort"]
            self.loaded = True
            self.message_key = "listener_ready"
        else:
            self.saved_url = result["baseUrl"]
            self.message_key = "listener_applied" if result["state"] == "running" else "listener_saved"
            self.applied.emit(result)
        self.busy = False
        self.save.setEnabled(self.loaded)
        self.cancel.setEnabled(True)
        self.host.setEnabled(True)
        self.port.setEnabled(True)
        self.retranslate(self.language)

    @Slot(str, str)
    def failed(self, command, error):
        self.busy = False
        self.message_key, self.raw_message = None, error
        self.save.setEnabled(self.loaded)
        self.cancel.setEnabled(True)
        self.host.setEnabled(True)
        self.port.setEnabled(True)
        self.render_message()

    def render_message(self):
        self.message.setText(tr(self.language, self.message_key, url=self.saved_url) if self.message_key else localize_message(self.language, self.raw_message))

    @Slot(str)
    def retranslate(self, language):
        self.language = language
        self.setWindowTitle(tr(language, "listener") + " - Any-Copilot-Proxy")
        self.heading.setText(tr(language, "listener"))
        self.description.setText(tr(language, "listener_description"))
        self.form_layout.labelForField(self.host).setText(tr(language, "listener_host"))
        self.form_layout.labelForField(self.port).setText(tr(language, "listener_port"))
        self.compatibility.setText(tr(language, "listener_compatibility", port=self.bridge_port))
        self.save.setText(tr(language, "save"))
        self.cancel.setText(tr(language, "cancel"))
        self.render_message()


class M365Dialog(QDialog):
    applied = Signal()

    def __init__(self, store, parent):
        super().__init__(parent)
        self.language = parent.language
        self.active_port = 0
        self.busy = False
        self.loaded = False
        self.raw_message = ""
        self.saved = False
        self.resize(540, 320)
        self.dispatch = Dispatcher(store, self)
        self.dispatch.result.connect(self.completed)
        self.dispatch.error.connect(self.failed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(16)
        self.description = label("", "muted")
        layout.addWidget(self.description)
        self.form = QFormLayout()
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.form.addRow(tr(self.language, "m365_control_port"), self.port)
        layout.addLayout(self.form)
        self.active = label("", "muted")
        self.message = label()
        layout.addWidget(self.active)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.save, self.cancel = QPushButton(), QPushButton()
        self.save.setObjectName("primary")
        self.save.setEnabled(False)
        self.save.clicked.connect(self.submit)
        self.cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)
        parent.language_changed.connect(self.retranslate)
        self.retranslate(self.language)
        self.dispatch.request("m365-settings-get", {"command": "m365-settings-get"})

    def reject(self):
        if not self.busy:
            super().reject()

    def submit(self):
        if self.busy or not self.loaded or not confirm(self, self.language, "m365_settings", "m365_port_confirm"):
            return
        self.busy = True
        self.save.setEnabled(False)
        self.port.setEnabled(False)
        self.dispatch.request("m365-settings-set", {"command": "m365-settings-set", "settings": {"controlPort": self.port.value()}})

    @Slot(str, object)
    def completed(self, command, result):
        self.active_port = result["activeControlPort"]
        self.port.setValue(result["controlPort"])
        self.loaded, self.busy = True, False
        self.saved = command == "m365-settings-set"
        self.raw_message = ""
        self.save.setEnabled(True)
        self.port.setEnabled(True)
        self.retranslate(self.language)
        if self.saved:
            self.applied.emit()

    @Slot(str, str)
    def failed(self, command, error):
        self.busy = False
        self.raw_message, self.saved = error, False
        self.save.setEnabled(self.loaded)
        self.port.setEnabled(True)
        self.retranslate(self.language)

    @Slot(str)
    def retranslate(self, language):
        self.language = language
        self.setWindowTitle(tr(language, "m365_settings"))
        self.description.setText(tr(language, "m365_port_description"))
        self.form.labelForField(self.port).setText(tr(language, "m365_control_port"))
        self.active.setText(tr(language, "m365_active_port", port=self.active_port))
        self.save.setText(tr(language, "save"))
        self.cancel.setText(tr(language, "cancel"))
        self.message.setText(localize_message(language, self.raw_message) if self.raw_message else
                             tr(language, "m365_port_saved") if self.saved else "")


class AntigravityDialog(QDialog):
    applied = Signal()

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.language = parent.language if isinstance(parent, MainWindow) else store.read("ui", {}).get("language", "zh")
        self.busy = False
        self.loaded = False
        self.saved = False
        self.raw_message = ""
        self.resize(540, 320)
        self.dispatch = Dispatcher(store, self)
        self.dispatch.result.connect(self.completed)
        self.dispatch.error.connect(self.failed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(14)
        self.description = label("", "muted")
        layout.addWidget(self.description)
        self.form = QFormLayout()
        self.binary_path_label = label()
        self.binary_path = QLineEdit()
        self.profile_mode_label = label()
        self.profile_mode = QComboBox()
        self.profile_mode.addItem("", "shared")
        self.profile_mode.addItem("", "isolated")
        self.form.addRow(self.binary_path_label, self.binary_path)
        self.form.addRow(self.profile_mode_label, self.profile_mode)
        layout.addLayout(self.form)
        self.message = label()
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.save = QPushButton()
        self.save.setObjectName("primary")
        self.save.setEnabled(False)
        self.cancel = QPushButton()
        buttons.addWidget(self.save)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)
        self.save.clicked.connect(self.submit)
        self.cancel.clicked.connect(self.reject)
        if isinstance(parent, MainWindow):
            parent.language_changed.connect(self.retranslate)
        self.retranslate(self.language)
        self.dispatch.request("agy-settings-get", {"command": "agy-settings-get"})

    def reject(self):
        if not self.busy:
            super().reject()

    def submit(self):
        if self.busy or not self.loaded:
            return
        self.busy = True
        self.save.setEnabled(False)
        self.binary_path.setEnabled(False)
        self.profile_mode.setEnabled(False)
        self.dispatch.request("agy-settings-set", {
            "command": "agy-settings-set",
            "settings": {
                "binaryPath": self.binary_path.text().strip(),
                "profileMode": self.profile_mode.currentData(),
            },
        })

    @Slot(str, object)
    def completed(self, command, result):
        self.binary_path.setText(result.get("binaryPath", ""))
        index = self.profile_mode.findData(result.get("profileMode", "shared"))
        if index >= 0:
            self.profile_mode.setCurrentIndex(index)
        self.loaded, self.busy = True, False
        self.saved = command == "agy-settings-set"
        self.raw_message = ""
        self.save.setEnabled(True)
        self.binary_path.setEnabled(True)
        self.profile_mode.setEnabled(True)
        self.retranslate(self.language)
        if self.saved:
            self.applied.emit()

    @Slot(str, str)
    def failed(self, command, error):
        self.busy = False
        self.raw_message, self.saved = error, False
        self.save.setEnabled(self.loaded)
        self.binary_path.setEnabled(True)
        self.profile_mode.setEnabled(True)
        self.retranslate(self.language)

    @Slot(str)
    def retranslate(self, language):
        self.language = language
        self.setWindowTitle(tr(language, "agy_settings"))
        self.description.setText(tr(language, "agy_settings_description"))
        self.binary_path_label.setText(tr(language, "agy_binary_path"))
        self.binary_path.setPlaceholderText(tr(language, "agy_binary_placeholder"))
        self.profile_mode_label.setText(tr(language, "agy_profile_mode"))
        self.profile_mode.setItemText(0, tr(language, "agy_profile_shared"))
        self.profile_mode.setItemText(1, tr(language, "agy_profile_isolated"))
        self.save.setText(tr(language, "save"))
        self.cancel.setText(tr(language, "cancel"))
        self.message.setText(localize_message(language, self.raw_message) if self.raw_message else
                             tr(language, "agy_settings_saved") if self.saved else "")


class MainWindow(QMainWindow):
    language_changed = Signal(str)

    def __init__(self, store, owns_manager=False, *, testing=False):
        super().__init__()
        self.store, self.owns_manager = store, owns_manager
        self.exiting = False
        self.pending_login = False
        self.setup_pending = False
        self.authenticated = False
        self.service_busy = False
        self.startup_busy = False
        self.m365_busy = False
        self.agy_busy = False
        self.logging_busy = False
        self.startup_enabled = None
        self.desktop_lock = None
        self.manager_thread = None
        self.language = store.read("ui", {}).get("language", "zh")
        if self.language not in TEXT:
            raise ValueError("Unsupported UI language; use zh or en in ui.json.")
        self.notice_key, self.raw_notice = "first_time", ""
        self.notice_source = None
        self.manager_connected = False
        self.last_status = None
        self.badge_code = "connecting"
        self.dispatch = Dispatcher(store, self)
        self.dispatch.result.connect(self.completed)
        self.dispatch.error.connect(self.failed)
        self.setWindowTitle("Any-Copilot-Proxy")
        self.setWindowIcon(app_icon())
        self.resize(880, 740)
        self.setMinimumSize(740, 550)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(20)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("Any-Copilot-Proxy", "heading"))
        self.subtitle = label("", "muted")
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.language_box = QComboBox()
        self.language_box.addItem("简体中文", "zh")
        self.language_box.addItem("English", "en")
        self.language_box.setCurrentIndex(self.language_box.findData(self.language))
        header.addWidget(self.language_box, alignment=Qt.AlignmentFlag.AlignTop)
        self.badge = label("CONNECTING", "badge")
        self.badge.setFixedHeight(36)
        header.addWidget(self.badge, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        card = QFrame()
        card.setObjectName("card")
        content = QVBoxLayout(card)
        content.setContentsMargins(24, 24, 24, 24)
        content.setSpacing(14)
        self.service_label = label()
        content.addWidget(self.service_label)
        self.address = label("http://127.0.0.1:4140/v1")
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.address.setStyleSheet("font-size:22px; font-weight:600;")
        content.addWidget(self.address)
        self.detail = label("", "muted")
        content.addWidget(self.detail)
        actions = QHBoxLayout()
        self.start_button, self.stop_button = QPushButton("Start service"), QPushButton("Stop")
        self.enable_startup_button = QPushButton()
        self.disable_startup_button = QPushButton()
        self.start_button.setObjectName("primary")
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.enable_startup_button)
        actions.addWidget(self.disable_startup_button)
        actions.addStretch()
        content.addLayout(actions)
        layout.addWidget(card)
        self.account = label("", "muted")
        layout.addWidget(self.account)
        row = QHBoxLayout()
        self.login_button = QPushButton("Sign in to GitHub")
        self.logout_button = QPushButton("Sign out")
        self.login_button.hide()
        self.logout_button.hide()
        self.proxy_button = QPushButton("Network settings")
        self.listener_button = QPushButton()
        for button in (self.login_button, self.logout_button, self.proxy_button, self.listener_button):
            row.addWidget(button)
        layout.addLayout(row)
        m365_row = QHBoxLayout()
        self.m365_switch = QCheckBox()
        self.m365_switch.setEnabled(False)
        self.m365_account = label("", "muted")
        self.m365_button = QPushButton()
        m365_row.addWidget(self.m365_switch)
        m365_row.addWidget(self.m365_account, 1)
        m365_row.addWidget(self.m365_button)
        layout.addLayout(m365_row)
        self.m365_switch.clicked.connect(self.toggle_m365)
        self.m365_button.clicked.connect(self.show_m365)
        agy_row = QHBoxLayout()
        self.agy_switch = QCheckBox()
        self.agy_switch.setEnabled(False)
        self.agy_account = label("", "muted")
        self.agy_install_button = QPushButton()
        self.agy_install_button.setObjectName("primary")
        self.agy_cancel_install_button = QPushButton()
        self.agy_login_button = QPushButton()
        self.agy_cancel_button = QPushButton()
        self.agy_logout_button = QPushButton()
        self.agy_settings_button = QPushButton()
        self.agy_install_button.hide()
        self.agy_cancel_install_button.hide()
        self.agy_login_button.hide()
        self.agy_cancel_button.hide()
        self.agy_logout_button.hide()
        agy_row.addWidget(self.agy_switch)
        agy_row.addWidget(self.agy_account, 1)
        agy_row.addWidget(self.agy_install_button)
        agy_row.addWidget(self.agy_cancel_install_button)
        agy_row.addWidget(self.agy_login_button)
        agy_row.addWidget(self.agy_cancel_button)
        agy_row.addWidget(self.agy_logout_button)
        agy_row.addWidget(self.agy_settings_button)
        layout.addLayout(agy_row)
        agy_progress_row = QHBoxLayout()
        self.agy_progress_bar = QProgressBar()
        self.agy_progress_bar.setRange(0, 100)
        self.agy_progress_bar.setFixedWidth(220)
        self.agy_progress_label = label("", "muted")
        self.agy_progress_bar.hide()
        self.agy_progress_label.hide()
        agy_progress_row.addWidget(self.agy_progress_bar)
        agy_progress_row.addWidget(self.agy_progress_label, 1)
        layout.addLayout(agy_progress_row)
        self.agy_switch.clicked.connect(self.toggle_agy)
        self.agy_install_button.clicked.connect(lambda: self.request("agy-install"))
        self.agy_cancel_install_button.clicked.connect(lambda: self.request("agy-cancel-install"))
        self.agy_login_button.clicked.connect(self.agy_login)
        self.agy_cancel_button.clicked.connect(lambda: self.request("agy-cancel-login"))
        self.agy_logout_button.clicked.connect(self.agy_logout)
        self.agy_settings_button.clicked.connect(self.show_agy)
        self.device_code = QLineEdit()
        self.device_code.setReadOnly(True)
        self.device_code.setPlaceholderText("Your device code will appear here")
        self.open_browser = QPushButton("Open GitHub authorization")
        self.cancel_login = QPushButton("Cancel sign-in")
        login_row = QHBoxLayout()
        for widget in (self.device_code, self.open_browser, self.cancel_login):
            login_row.addWidget(widget)
            widget.hide()
        layout.addLayout(login_row)
        self.notice = label("First time? Configure a proxy if needed, then choose Finish setup.", "muted")
        layout.addWidget(self.notice)
        layout.addStretch()
        footer = QHBoxLayout()
        self.setup_button = QPushButton("Finish setup / enable login startup")
        self.remove_button = QPushButton("Uninstall")
        self.remove_button.setObjectName("danger")
        footer.addWidget(self.setup_button)
        self.logging_switch = QCheckBox()
        self.logging_switch.setEnabled(False)
        self.logs_button = QPushButton()
        footer.addWidget(self.logging_switch)
        footer.addWidget(self.logs_button)
        footer.addStretch()
        footer.addWidget(self.remove_button)
        layout.addLayout(footer)
        self.start_button.clicked.connect(lambda: self.request("start"))
        self.stop_button.clicked.connect(self.stop_service)
        self.enable_startup_button.clicked.connect(self.enable_startup)
        self.disable_startup_button.clicked.connect(self.disable_startup)
        self.login_button.clicked.connect(self.login)
        self.logout_button.clicked.connect(self.logout)
        self.proxy_button.clicked.connect(self.show_proxy)
        self.listener_button.clicked.connect(self.show_listener)
        self.setup_button.clicked.connect(self.setup)
        self.remove_button.clicked.connect(self.uninstall)
        self.logging_switch.clicked.connect(self.toggle_logging)
        self.logs_button.clicked.connect(self.open_logs)
        self.open_browser.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/login/device")))
        self.cancel_login.clicked.connect(lambda: self.request("cancel-login"))
        self.tray = None
        self.tray_actions = {}
        if not testing and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon(), self)
            self.tray.setToolTip("Any-Copilot-Proxy")
            menu = QMenu()
            for key, callback in (("dashboard", self.showNormal), ("network", self.show_proxy), ("listener", self.show_listener), ("quit", self.quit)):
                action = menu.addAction(tr(self.language, key))
                action.triggered.connect(callback)
                self.tray_actions[key] = action
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
            self.tray.show()
        self.retranslate()
        self.language_box.currentIndexChanged.connect(self.change_language)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(lambda: self.request("status"))
        if not testing:
            self.timer.start()
            self.request("status")
            self.refresh_startup()

    def request(self, command):
        if command in ("start", "stop"):
            self.service_busy = True
            self.render_service_controls()
        self.dispatch.request(command, {"command": command})

    def stop_service(self):
        if confirm(self, self.language, "stop_title", "stop_body"):
            self.request("stop")

    def refresh_startup(self):
        if not self.startup_busy:
            self.dispatch.work("startup-status", lambda: startup_enabled(self.store))

    def enable_startup(self):
        self.change_startup(True)

    def disable_startup(self):
        if confirm(self, self.language, "disable_startup_title", "disable_startup_body"):
            self.change_startup(False)

    def change_startup(self, enabled, *, start_service=False):
        self.startup_busy = True
        self.render_startup_controls()
        command = "startup" if start_service else "startup-enable" if enabled else "startup-disable"

        def apply():
            if enabled:
                install_startup(self.store, True)
            else:
                remove_startup(self.store)
            actual = startup_enabled(self.store)
            if actual != enabled:
                raise RuntimeError("Login startup change could not be verified.")
            return actual
        self.dispatch.work(command, apply)

    def login(self):
        self.pending_login = True
        self.request("login")

    def logout(self):
        if confirm(self, self.language, "logout_title", "logout_body"):
            self.pending_login = False
            self.request("logout")

    def setup(self):
        if self.authenticated:
            if self.startup_enabled:
                self.request("start")
            else:
                self.change_startup(True, start_service=True)
        else:
            self.setup_pending = True
            self.login()

    def show_proxy(self):
        dialog = ProxyDialog(self.store, self)
        dialog.setStyleSheet(STYLE)
        dialog.exec()

    def show_listener(self):
        dialog = ListenerDialog(self.store, self)
        dialog.setStyleSheet(STYLE)
        dialog.applied.connect(lambda result: self.completed("listener-set", result))
        dialog.exec()

    def show_m365(self):
        dialog = M365Dialog(self.store, self)
        dialog.setStyleSheet(STYLE)
        dialog.applied.connect(lambda: self.request("status"))
        dialog.exec()

    def toggle_m365(self, enabled):
        self.m365_busy = True
        self.m365_switch.setEnabled(False)
        self.dispatch.request("m365-settings-set", {"command": "m365-settings-set", "settings": {"enabled": enabled}})

    def show_agy(self):
        dialog = AntigravityDialog(self.store, self)
        dialog.setStyleSheet(STYLE)
        dialog.applied.connect(lambda: self.request("status"))
        dialog.exec()

    def toggle_agy(self, enabled):
        self.agy_busy = True
        self.agy_switch.setEnabled(False)
        self.dispatch.request("agy-settings-set", {"command": "agy-settings-set", "settings": {"enabled": enabled}})

    def agy_login(self):
        self.request("agy-login")

    def agy_logout(self):
        if confirm(self, self.language, "agy_logout_title", "agy_logout_body"):
            self.request("agy-logout")

    def toggle_logging(self, enabled):
        self.logging_busy = True
        self.logging_switch.setEnabled(False)
        self.dispatch.request("logging-set", {"command": "logging-set", "enabled": enabled})

    def open_logs(self):
        directory = self.store.directory / "logs"
        if not directory.is_dir():
            inform(self, self.language, "open_logs", "logs_not_created")
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            self.set_message(tr(self.language, "logs_open_failed"))

    @Slot(str, object)
    def completed(self, command, result):
        if command == "logging-set":
            self.logging_busy = False
        if command == "m365-settings-set":
            self.m365_busy = False
            self.m365_switch.setChecked(result["enabled"])
            self.request("status")
            return
        if command == "agy-settings-set":
            self.agy_busy = False
            self.agy_switch.setChecked(result["enabled"])
            self.request("status")
            return
        if command in ("startup-status", "startup", "startup-enable", "startup-disable"):
            if command == "startup-status" and self.startup_busy:
                return
            self.startup_enabled = result
            if command != "startup-status":
                self.startup_busy = False
                self.set_notice("startup_disabled" if command == "startup-disable" else
                                "startup_enabled" if command == "startup" else "startup_enabled_only")
            self.render_startup_controls()
            if command == "startup":
                self.request("start")
            return
        if command == "cleanup":
            self.exiting = True
            inform(self, self.language, "uninstall_done", "cleanup_body", root=self.store.root)
            QApplication.instance().quit()
            return
        if command == "shutdown":
            self.exiting = True
            QApplication.instance().quit()
            return
        self.manager_connected = True
        if self.notice_source in ("status", "manager", "connection"):
            self.set_notice("control_connected")
        self.last_status = result
        self.badge_code = result["state"]
        if command in ("start", "stop"):
            self.service_busy = False
        self.render_status()
        self.authenticated = result.get("githubAuthenticated", result["authenticated"])
        pending = result["loginState"] == "pending"
        self.logout_button.setVisible(self.authenticated)
        self.logout_button.setEnabled(self.authenticated)
        self.login_button.setVisible(not self.authenticated and not pending)
        self.login_button.setEnabled(not self.authenticated and not pending)
        for widget in (self.device_code, self.open_browser, self.cancel_login):
            widget.setVisible(pending)
        self.device_code.setText(result.get("deviceCode") or "")
        if self.pending_login and result["loginState"] == "complete":
            self.pending_login = False
            if self.setup_pending:
                self.setup_pending = False
                self.setup()
            else:
                self.request("start")

    @Slot(str, str)
    def failed(self, command, error):
        self.set_message(error, source=command)
        connection_failed = command == "status" or error in CONTROL_CONNECTION_ERRORS
        if command == "m365-settings-set":
            self.m365_busy = False
            self.render_m365_controls()
        if command == "agy-settings-set":
            self.agy_busy = False
            self.render_agy_controls()
        if command == "logging-set":
            self.logging_busy = False
            self.render_logging_controls()
        if connection_failed:
            self.manager_connected = False
            self.authenticated = False
            self.badge_code = "unreachable"
            self.login_button.hide()
            self.logout_button.hide()
            for widget in (self.device_code, self.open_browser, self.cancel_login):
                widget.hide()
            self.render_status()
        if command in ("start", "stop"):
            self.service_busy = False
            self.render_service_controls()
            self.request("status")
        if command in ("startup-status", "startup", "startup-enable", "startup-disable"):
            self.startup_busy = False
            self.startup_enabled = None
            self.render_startup_controls()
            if command != "startup-status":
                self.refresh_startup()
        if command == "cleanup":
            self.timer.start()
        if command == "shutdown":
            if confirm(self, self.language, "exit_title", "exit_body", message=localize_message(self.language, error)):
                self.exiting = True
                QApplication.instance().quit()

    def uninstall(self):
        if not confirm(self, self.language, "uninstall_title", "uninstall_body"):
            return
        self.timer.stop()

        def clean():
            control_request(self.store, {"command": "shutdown"})
            for _ in range(50):
                if not self.store.read("control"):
                    if self.manager_thread:
                        self.manager_thread.join(timeout=10)
                        if self.manager_thread.is_alive():
                            raise RuntimeError("The application is still stopping; retry cleanup.")
                    if self.desktop_lock:
                        self.desktop_lock.close()
                    return clean_installation(self.store)
                time.sleep(0.1)
            raise RuntimeError("Service is still stopping; cleanup was not performed.")
        self.dispatch.work("cleanup", clean)

    def quit(self):
        self.timer.stop()
        if self.owns_manager:
            self.request("shutdown")
        else:
            self.exiting = True
            QApplication.instance().quit()

    def closeEvent(self, event):
        if self.tray and not self.exiting:
            self.hide()
            event.ignore()
        elif not self.exiting and self.owns_manager:
            event.ignore()
            self.quit()
        else:
            event.accept()


    def set_notice(self, key):
        self.notice_key = key
        self.raw_notice = ""
        self.notice_source = None
        self.notice.setText(tr(self.language, key))

    def set_message(self, message, *, source=None):
        self.notice_key, self.raw_notice = None, message
        self.notice_source = "connection" if message in CONTROL_CONNECTION_ERRORS else source
        self.notice.setText(localize_message(self.language, message))

    def render_status(self):
        self.badge.setText(TEXT[self.language].get(self.badge_code, self.badge_code))
        if not self.manager_connected:
            self.account.setText(tr(self.language, "account_unavailable") if self.badge_code == "unreachable" else tr(self.language, "account_checking"))
            self.detail.setText(tr(self.language, "control_state_unknown") if self.badge_code == "unreachable" else tr(self.language, "checking"))
        elif self.last_status:
            if self.last_status["loginState"] == "pending":
                account_text = tr(self.language, "signing-in")
            elif self.last_status.get("githubAuthenticated", self.last_status["authenticated"]):
                username = self.last_status.get("username")
                account_text = tr(self.language, "signed_in_as", username=username) if username else tr(self.language, "login_success")
            else:
                account_text = tr(self.language, "signed-out")
            self.account.setText(account_text)
            self.address.setText(self.last_status["baseUrl"])
            self.detail.setText(localize_message(self.language, self.last_status.get("message", "")) or
                                tr(self.language, "ready" if self.badge_code == "running" else "not_ready"))
        else:
            self.account.setText(tr(self.language, "account_checking"))
            self.detail.setText(tr(self.language, "checking"))
        self.render_service_controls()
        self.render_startup_controls()
        self.render_m365_controls()
        self.render_agy_controls()
        self.render_logging_controls()

    def render_logging_controls(self):
        settings = (self.last_status or {}).get("logging")
        self.logging_switch.setEnabled(self.manager_connected and settings is not None and not self.logging_busy)
        if settings is not None and not self.logging_busy:
            self.logging_switch.setChecked(settings["enabled"])

    def render_m365_controls(self):
        settings = (self.last_status or {}).get("m365")
        self.m365_switch.setEnabled(self.manager_connected and settings is not None and not self.m365_busy)
        self.m365_button.setEnabled(self.manager_connected and not self.m365_busy)
        if settings is not None and not self.m365_busy:
            self.m365_switch.setChecked(settings["syncEnabled"])
        self.m365_account.setText(settings.get("username") or tr(self.language, "signed-out") if settings else "")

    def render_agy_controls(self):
        info = (self.last_status or {}).get("antigravity")
        connected = self.manager_connected and info is not None
        self.agy_switch.setEnabled(connected and not self.agy_busy)
        self.agy_settings_button.setEnabled(self.manager_connected and not self.agy_busy)
        if not connected:
            for widget in (self.agy_install_button, self.agy_cancel_install_button,
                           self.agy_login_button, self.agy_cancel_button, self.agy_logout_button,
                           self.agy_progress_bar, self.agy_progress_label):
                widget.hide()
            self.agy_account.setText("")
            return
        if not self.agy_busy:
            self.agy_switch.setChecked(info["enabled"])
        install = info.get("install") or {}
        install_state = install.get("state", "idle")
        installing = install_state == "running"
        pending = info.get("loginState") == "pending"
        authed = bool(info.get("authenticated"))
        found = bool(info.get("binaryFound"))

        if self.timer.isActive():
            desired = 250 if installing else 1500
            if self.timer.interval() != desired:
                self.timer.setInterval(desired)

        self.agy_install_button.setVisible(not found and not installing)
        self.agy_install_button.setEnabled(not installing and not self.agy_busy)
        self.agy_cancel_install_button.setVisible(installing)
        self.agy_cancel_install_button.setEnabled(installing)
        self.agy_login_button.setVisible(found and not authed and not pending and not installing)
        self.agy_login_button.setEnabled(found and not self.agy_busy and not installing)
        self.agy_cancel_button.setVisible(pending and not installing)
        self.agy_cancel_button.setEnabled(pending)
        self.agy_logout_button.setVisible(authed and not pending and not installing)
        self.agy_logout_button.setEnabled(authed and not self.agy_busy)

        if installing:
            text = self._format_agy_install_text(install)
            stage = install.get("stage", "manifest")
            if stage in ("manifest", "configuring") or (stage == "downloading" and not install.get("totalBytes")):
                self.agy_progress_bar.setRange(0, 0)
            else:
                self.agy_progress_bar.setRange(0, 100)
                self.agy_progress_bar.setValue(int(install.get("percent", 0)))
            self.agy_progress_bar.show()
            self.agy_progress_label.setText(text)
            self.agy_progress_label.show()
            self.agy_account.setText(tr(self.language, "working"))
            return

        if install_state in ("complete", "failed", "cancelled"):
            self.agy_progress_bar.setRange(0, 100)
            self.agy_progress_bar.setValue(100 if install_state == "complete" else int(install.get("percent", 0)))
            self.agy_progress_bar.setVisible(install_state == "complete")
            self.agy_progress_label.setText(self._format_agy_install_text(install))
            self.agy_progress_label.show()
        else:
            self.agy_progress_bar.hide()
            self.agy_progress_label.hide()

        if pending:
            text = tr(self.language, "agy_signing_in")
        elif not found:
            text = tr(self.language, "agy_not_installed")
        elif authed:
            account = info.get("account")
            text = tr(self.language, "signed_in_as", username=account) if account else tr(self.language, "login_success")
        else:
            text = tr(self.language, "signed-out")
        self.agy_account.setText(text)

    def _format_agy_install_text(self, install: dict) -> str:
        stage = install.get("stage", "idle")
        version = install.get("version") or ""
        if version and not version.startswith("v"):
            version = f"v{version}"
        if stage == "downloading":
            downloaded = f"{install.get('downloadedBytes', 0) / 1048576:.1f}"
            total_bytes = install.get("totalBytes", 0)
            if total_bytes > 0:
                total = f"{total_bytes / 1048576:.1f}"
                return tr(self.language, "agy_stage_downloading", version=version, downloaded=downloaded, total=total, percent=int(install.get("percent", 0)))
            return tr(self.language, "agy_stage_downloading_unknown", version=version, downloaded=downloaded)
        if stage == "failed":
            return tr(self.language, "agy_stage_failed", error=localize_message(self.language, install.get("error", "")))
        if stage == "complete":
            return tr(self.language, "agy_stage_complete", version=version)
        key = f"agy_stage_{stage}"
        if key in TEXT[self.language]:
            return tr(self.language, key)
        return tr(self.language, "working")

    def render_service_controls(self):
        state = self.badge_code
        show_stop = state in ("running", "stopping")
        self.start_button.setVisible(not show_stop)
        self.stop_button.setVisible(show_stop)
        can_start = bool(self.last_status and self.last_status["authenticated"] and
                         self.last_status["loginState"] != "pending" and state in ("stopped", "error"))
        self.start_button.setEnabled(can_start and not self.service_busy)
        self.stop_button.setEnabled(state == "running" and not self.service_busy)
        self.listener_button.setEnabled(self.manager_connected and not self.service_busy and state not in ("starting", "stopping"))
        self.proxy_button.setEnabled(self.manager_connected)

    def render_startup_controls(self):
        self.enable_startup_button.setVisible(self.startup_enabled is not True)
        self.disable_startup_button.setVisible(self.startup_enabled is True)
        enabled = self.startup_enabled is not None and not self.startup_busy
        self.enable_startup_button.setEnabled(enabled)
        self.disable_startup_button.setEnabled(enabled)
        authenticated = bool(self.last_status and self.last_status["authenticated"])
        self.setup_button.setVisible(not authenticated)
        self.setup_button.setEnabled(self.manager_connected and enabled and not self.service_busy and
                                     not (self.last_status and self.last_status["loginState"] == "pending"))

    @Slot(int)
    def change_language(self, index):
        language = self.language_box.itemData(index)
        try:
            self.store.ensure()
            self.store.write("ui", {"language": language})
        except Exception as error:
            self.set_message(safe_error(error))
            self.language_box.blockSignals(True)
            self.language_box.setCurrentIndex(self.language_box.findData(self.language))
            self.language_box.blockSignals(False)
            return
        self.language = language
        self.retranslate()
        self.language_changed.emit(language)

    def retranslate(self):
        self.subtitle.setText(tr(self.language, "subtitle"))
        self.service_label.setText(tr(self.language, "service"))
        for key, button in (("start", self.start_button), ("stop", self.stop_button), ("login", self.login_button),
                            ("logout", self.logout_button), ("network", self.proxy_button), ("open_github", self.open_browser),
                            ("cancel_login", self.cancel_login), ("setup", self.setup_button), ("uninstall", self.remove_button),
                            ("enable_startup", self.enable_startup_button), ("disable_startup", self.disable_startup_button),
                            ("listener", self.listener_button)):
            button.setText(tr(self.language, key))
        self.m365_switch.setText(tr(self.language, "m365_sync"))
        self.m365_switch.setToolTip(tr(self.language, "m365_sync_hint"))
        self.m365_button.setText(tr(self.language, "m365_settings"))
        self.agy_switch.setText(tr(self.language, "agy_enable"))
        self.agy_switch.setToolTip(tr(self.language, "agy_enable_hint"))
        self.agy_install_button.setText(tr(self.language, "agy_install"))
        self.agy_cancel_install_button.setText(tr(self.language, "agy_cancel_install"))
        self.agy_login_button.setText(tr(self.language, "agy_login"))
        self.agy_cancel_button.setText(tr(self.language, "agy_cancel_login"))
        self.agy_logout_button.setText(tr(self.language, "agy_logout"))
        self.agy_settings_button.setText(tr(self.language, "agy_settings"))
        self.logging_switch.setText(tr(self.language, "runtime_logs"))
        self.logging_switch.setToolTip(tr(self.language, "runtime_logs_hint"))
        self.logs_button.setText(tr(self.language, "open_logs"))
        self.enable_startup_button.setToolTip(tr(self.language, "startup_tooltip"))
        self.disable_startup_button.setToolTip(tr(self.language, "startup_tooltip"))
        self.device_code.setPlaceholderText(tr(self.language, "device_placeholder"))
        for key, action in self.tray_actions.items():
            action.setText(tr(self.language, key))
        self.notice.setText(tr(self.language, self.notice_key) if self.notice_key else localize_message(self.language, self.raw_notice))
        self.render_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    store = UserStore(args.root)
    store.ensure()
    lock = InstanceLock(store, "desktop")
    if not lock.acquire():
        return 0
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Any-Copilot-Proxy")
    app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    app.setStyleSheet(STYLE)
    try:
        control_request(store, {"command": "status"}, 3)
        owns = False
    except (ConnectionError, OSError):
        owns = True
    errors = []
    thread = None
    if owns:
        def run():
            try:
                asyncio.run(serve(store))
            except Exception as error:
                errors.append(safe_error(error))
        thread = threading.Thread(target=run, daemon=True, name="Any-Copilot-Proxy")
        thread.start()
    window = MainWindow(store, owns)
    window.desktop_lock = lock
    window.manager_thread = thread
    if args.setup:
        window.set_notice("setup_notice")
    error_timer = QTimer(window)
    error_timer.timeout.connect(lambda: window.set_message(errors.pop(), source="manager") if errors else None)
    error_timer.start(1000)
    window.show()
    result = app.exec()
    if window.tray:
        window.tray.hide()
    if thread:
        thread.join(timeout=10)
    lock.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
