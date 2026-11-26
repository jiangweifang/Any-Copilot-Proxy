import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
    from PySide6.QtTest import QTest
    from copilot_proxy.desktop import Dispatcher, ListenerDialog, M365Dialog, MainWindow, ProxyDialog
    from copilot_proxy.config import UserStore
    from copilot_proxy.runtime import CONTROL_UNAVAILABLE
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "Desktop extra is not installed")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_logging_switch_applies_live_and_pending_status_does_not_revert_the_click(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            status = {"state": "running", "baseUrl": "http://127.0.0.1:4140/v1", "authenticated": True,
                      "loginState": "idle", "message": "", "deviceCode": None,
                      "logging": {"enabled": True, "active": True}}
            window.completed("status", status)
            self.assertEqual(window.logging_switch.text(), "记录运行日志")
            self.assertTrue(window.logging_switch.isChecked())
            window.logging_switch.click()
            self.assertEqual(request.call_args.args[1], {"command": "logging-set", "enabled": False})
            window.completed("status", status)
            self.assertFalse(window.logging_switch.isChecked())
            self.assertFalse(window.logging_switch.isEnabled())
            window.completed("logging-set", {**status, "logging": {"enabled": False, "active": False}})
            self.assertFalse(window.logging_switch.isChecked())
            self.assertTrue(window.logging_switch.isEnabled())
            self.assertEqual(window.badge.text(), "运行中")
            window.close()
            window.deleteLater()

    def test_m365_switch_preserves_login_and_port_dialog_does_not_replace_dashboard(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            status = {"state": "running", "baseUrl": "http://127.0.0.1:4140/v1", "authenticated": True,
                      "loginState": "idle", "message": "", "deviceCode": None,
                      "m365": {"syncEnabled": True, "username": "name@example.invalid"}}
            window.completed("status", status)
            self.assertTrue(window.m365_switch.isChecked())
            self.assertEqual(window.m365_account.text(), "name@example.invalid")
            window.m365_switch.click()
            self.assertEqual(request.call_args.args[1], {"command": "m365-settings-set", "settings": {"enabled": False}})
            self.assertTrue(window.authenticated)
            dialog = M365Dialog(window.store, window)
            dialog.completed("m365-settings-get", {"enabled": False, "controlPort": 0, "activeControlPort": 43210})
            self.assertEqual(window.badge.text(), "运行中")
            self.assertIn("43210", dialog.active.text())
            dialog.port.setValue(43211)
            with patch("copilot_proxy.desktop.confirm", return_value=True):
                dialog.submit()
            self.assertEqual(request.call_args.args[1]["settings"], {"controlPort": 43211})
            dialog.completed("m365-settings-set", {"enabled": False, "controlPort": 43211, "activeControlPort": 43210})
            self.assertIn("重启", dialog.message.text())
            for widget in (dialog, window):
                widget.close()
                widget.deleteLater()

    def test_open_proxy_dialogs_do_not_shadow_dashboard_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            store = UserStore(Path(directory), Path(directory) / "user")
            window = MainWindow(store, testing=True)
            first, second = ProxyDialog(store, window), ProxyDialog(store, window)
            status = {"state": "running", "baseUrl": "http://127.0.0.1:4140/v1", "authenticated": True,
                      "loginState": "idle", "message": "", "deviceCode": None}
            window.dispatch.result.emit("status", status)
            first.dispatch.result.emit("proxy-get", {"type": "socks5", "host": "localhost", "port": 1080, "hasPassword": True})
            second.dispatch.error.emit("proxy-test", "Connection unavailable")
            QTest.qWait(10)
            self.assertEqual(window.badge.text(), "运行中")
            self.assertEqual(first.kind.currentData(), "socks5")
            self.assertEqual(first.password.echoMode(), QLineEdit.EchoMode.Password)
            self.assertTrue(first.keep.isChecked())
            self.assertIn("unavailable", second.message.text())
            self.assertEqual(window.address.text(), status["baseUrl"])
            for widget in (first, second, window):
                widget.close()
                widget.deleteLater()

    def test_language_switch_updates_open_dialogs_and_persists_without_changing_proxy(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request:
            store = UserStore(Path(directory), Path(directory) / "user")
            store.ensure()
            store.write("settings", {"proxy": {"type": "none"}})
            window = MainWindow(store, testing=True)
            dialog = ProxyDialog(store, window)
            self.assertEqual(window.language, "zh")
            self.assertEqual(window.start_button.text(), "启动服务")
            self.assertEqual(dialog.save.text(), "保存并应用")
            dialog.kind.setCurrentIndex(dialog.kind.findData("socks5"))
            dialog.host.setText("localhost")
            dialog.port.setValue(1080)
            window.language_box.setCurrentIndex(window.language_box.findData("en"))
            self.assertEqual(window.start_button.text(), "Start service")
            self.assertEqual(dialog.save.text(), "Save and apply")
            self.assertEqual(dialog.kind.currentData(), "socks5")
            self.assertEqual(dialog.host.text(), "localhost")
            dialog.submit("proxy-test")
            self.assertEqual(request.call_args.args[1]["proxy"]["type"], "socks5")
            self.assertEqual(store.read("ui"), {"language": "en"})
            self.assertEqual(store.read("settings"), {"proxy": {"type": "none"}})
            restored = MainWindow(store, testing=True)
            self.assertEqual(restored.language, "en")
            self.assertEqual(restored.login_button.text(), "Sign in to GitHub")
            window.language_box.setCurrentIndex(window.language_box.findData("zh"))
            self.assertEqual(dialog.test.text(), "测试连接")
            self.assertEqual(window.login_button.text(), "登录 GitHub")
            for widget in (dialog, window, restored):
                widget.close()
                widget.deleteLater()

    def test_background_completion_returns_to_the_gui_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = Dispatcher(UserStore(Path(directory)))
            result = []
            dispatcher.result.connect(lambda key, value: result.append((key, value)))
            dispatcher.work("probe", lambda: {"ready": True})
            for _ in range(50):
                if result: break
                QTest.qWait(20)
            self.assertEqual(result, [("probe", {"ready": True})])
            self.assertFalse(dispatcher.jobs)

    def test_logout_requires_confirmation_and_defaults_to_no(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            window.pending_login = True
            def decline(box):
                self.assertEqual(box.windowTitle(), "退出登录")
                self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
                self.assertEqual(box.button(QMessageBox.StandardButton.No).text(), "否")
                return QMessageBox.StandardButton.No
            with patch.object(QMessageBox, "exec", decline):
                window.logout()
            request.assert_not_called()
            self.assertTrue(window.pending_login)
            with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
                window.logout()
            request.assert_called_once_with("logout", {"command": "logout"})
            self.assertFalse(window.pending_login)
            window.close()
            window.deleteLater()

    def test_signed_in_username_fallback_and_signout_follow_selected_language(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            store = UserStore(Path(directory), Path(directory) / "user")
            window = MainWindow(store, testing=True)
            status = {"state": "running", "baseUrl": "http://127.0.0.1:4140/v1", "authenticated": True,
                      "loginState": "complete", "message": "", "deviceCode": None, "username": "test-user"}
            window.completed("status", status)
            self.assertEqual(window.account.text(), "已登录：test-user")
            window.language_box.setCurrentIndex(window.language_box.findData("en"))
            self.assertEqual(window.account.text(), "Signed in as test-user")
            window.completed("status", {**status, "username": None})
            self.assertEqual(window.account.text(), "Signed in")
            window.language_box.setCurrentIndex(window.language_box.findData("zh"))
            self.assertEqual(window.account.text(), "登录成功")
            window.completed("logout", {**status, "state": "signed-out", "authenticated": False, "loginState": "idle", "username": None})
            self.assertEqual(window.account.text(), "未登录")
            window.close()
            window.deleteLater()

    def test_login_button_only_appears_when_signed_out_and_not_authorizing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            store = UserStore(Path(directory), Path(directory) / "user")
            window = MainWindow(store, testing=True)
            self.assertTrue(window.login_button.isHidden())
            self.assertTrue(window.logout_button.isHidden())
            base = {"baseUrl": "http://127.0.0.1:4140/v1", "message": "", "deviceCode": None}
            for state, authenticated, login_state, show_login, show_logout in (
                ("signed-out", False, "idle", True, False),
                ("signing-in", False, "pending", False, False),
                ("running", True, "complete", False, True),
                ("stopped", True, "idle", False, True),
                ("signed-out", False, "idle", True, False),
            ):
                with self.subTest(state=state, authenticated=authenticated):
                    window.completed("status", {**base, "state": state, "authenticated": authenticated,
                                                "loginState": login_state, "username": None})
                    self.assertEqual(not window.login_button.isHidden(), show_login)
                    self.assertEqual(not window.logout_button.isHidden(), show_logout)
            window.completed("status", {**base, "state": "running", "authenticated": True,
                                        "loginState": "complete", "username": "test-user"})
            window.language_box.setCurrentIndex(window.language_box.findData("en"))
            self.assertTrue(window.login_button.isHidden())
            self.assertFalse(window.logout_button.isHidden())
            window.close()
            window.deleteLater()

    def test_service_buttons_are_mutually_exclusive_and_busy_states_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            window = MainWindow(UserStore(Path(directory)), testing=True)
            self.assertFalse(window.start_button.isHidden())
            self.assertTrue(window.stop_button.isHidden())
            self.assertFalse(window.start_button.isEnabled())
            for state, authenticated, show_stop, can_act in (
                ("signed-out", False, False, False),
                ("stopped", True, False, True),
                ("starting", True, False, False),
                ("running", True, True, True),
                ("stopping", True, True, False),
                ("error", True, False, True),
            ):
                with self.subTest(state=state):
                    window.completed("status", {"state": state, "authenticated": authenticated, "loginState": "idle",
                                                "baseUrl": "http://127.0.0.1:4140/v1", "message": "", "deviceCode": None})
                    self.assertEqual(window.start_button.isHidden(), show_stop)
                    self.assertEqual(window.stop_button.isHidden(), not show_stop)
                    active = window.stop_button if show_stop else window.start_button
                    self.assertEqual(active.isEnabled(), can_act)
            window.request("start")
            self.assertFalse(window.start_button.isEnabled())
            window.close()
            window.deleteLater()

    def test_stop_disable_startup_and_uninstall_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request, \
             patch.object(Dispatcher, "work") as work:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            for action, title in ((window.stop_service, "停止服务"), (window.disable_startup, "禁用自启动"), (window.uninstall, "卸载此安装")):
                with self.subTest(action=title):
                    request.reset_mock()
                    work.reset_mock()
                    window.timer.start()
                    def decline(box):
                        self.assertEqual(box.windowTitle(), title)
                        self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
                        return QMessageBox.StandardButton.No
                    with patch.object(QMessageBox, "exec", decline):
                        action()
                    request.assert_not_called()
                    work.assert_not_called()
                    self.assertTrue(window.timer.isActive())
                    with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes):
                        action()
                    if title == "停止服务":
                        request.assert_called_once_with("stop", {"command": "stop"})
                    else:
                        self.assertEqual(work.call_args.args[0], "startup-disable" if title == "禁用自启动" else "cleanup")
                        request.assert_not_called()
            window.timer.stop()
            window.close()
            window.deleteLater()

    def test_startup_toggle_verifies_state_without_starting_or_stopping_service(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request, \
             patch.object(Dispatcher, "work") as work, patch("copilot_proxy.desktop.install_startup") as install, \
             patch("copilot_proxy.desktop.remove_startup") as remove, patch("copilot_proxy.desktop.startup_enabled") as enabled:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            window.completed("status", {"state": "running", "authenticated": True, "loginState": "idle",
                                        "baseUrl": "http://127.0.0.1:4140/v1", "message": "", "deviceCode": None})
            window.completed("startup-status", False)
            self.assertFalse(window.enable_startup_button.isHidden())
            self.assertTrue(window.disable_startup_button.isHidden())
            window.enable_startup()
            self.assertFalse(window.enable_startup_button.isEnabled())
            key, apply = work.call_args.args
            enabled.return_value = True
            window.completed(key, apply())
            install.assert_called_once_with(window.store, True)
            self.assertTrue(window.enable_startup_button.isHidden())
            self.assertFalse(window.disable_startup_button.isHidden())
            self.assertTrue(window.disable_startup_button.isEnabled())
            with patch("copilot_proxy.desktop.confirm", return_value=True):
                window.disable_startup()
            key, apply = work.call_args.args
            enabled.return_value = False
            window.completed(key, apply())
            remove.assert_called_once_with(window.store)
            self.assertFalse(window.enable_startup_button.isHidden())
            self.assertTrue(window.disable_startup_button.isHidden())
            self.assertEqual(window.badge.text(), "运行中")
            request.assert_not_called()
            window.language_box.setCurrentIndex(window.language_box.findData("en"))
            self.assertEqual(window.enable_startup_button.text(), "Enable login startup")
            self.assertEqual(window.stop_button.text(), "Stop service")
            window.close()
            window.deleteLater()

    def test_startup_failure_rechecks_actual_state_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request, \
             patch.object(Dispatcher, "work") as work:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            window.completed("startup-status", True)
            window.startup_busy = True
            window.failed("startup-disable", "Permission denied")
            self.assertFalse(window.enable_startup_button.isEnabled())
            self.assertEqual(work.call_args.args[0], "startup-status")
            window.completed("startup-status", True)
            self.assertFalse(window.disable_startup_button.isHidden())
            self.assertTrue(window.disable_startup_button.isEnabled())
            self.assertEqual(window.notice.text(), "Permission denied")
            request.assert_not_called()
            window.close()
            window.deleteLater()

    def test_listener_dialog_validates_confirms_and_preserves_unrelated_fields(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request") as request:
            window = MainWindow(UserStore(Path(directory)), testing=True)
            dialog = ListenerDialog(window.store, window)
            self.assertFalse(dialog.save.isEnabled())
            dialog.completed("listener-get", {"host": "127.0.0.1", "port": 4140, "bridgePort": 4141})
            request.reset_mock()
            dialog.port.setValue(4141)
            with patch("copilot_proxy.desktop.confirm") as confirmation:
                dialog.submit()
            confirmation.assert_not_called()
            request.assert_not_called()
            self.assertIn("端口", dialog.message.text())
            dialog.port.setValue(4240)
            dialog.host.setText("127.0.0.2")
            with patch("copilot_proxy.desktop.confirm", return_value=False):
                dialog.submit()
            request.assert_not_called()
            with patch("copilot_proxy.desktop.confirm", return_value=True):
                dialog.submit()
            request.assert_called_once_with("listener-set", {"command": "listener-set", "listener": {"host": "127.0.0.2", "port": 4240}})
            self.assertFalse(dialog.save.isEnabled())
            self.assertFalse(dialog.cancel.isEnabled())
            applied = []
            dialog.applied.connect(applied.append)
            result = {"state": "running", "baseUrl": "http://127.0.0.2:4240/v1"}
            dialog.completed("listener-set", result)
            self.assertEqual(applied, [result])
            self.assertIn(result["baseUrl"], dialog.message.text())
            dialog.retranslate("en")
            self.assertEqual(dialog.save.text(), "Save and apply")
            self.assertIn("4141", dialog.compatibility.text())
            dialog.close()
            dialog.deleteLater()
            window.close()
            window.deleteLater()

    def test_initial_connection_warning_is_cleared_after_status_recovers(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            window = MainWindow(UserStore(Path(directory)), testing=True)
            window.failed("status", CONTROL_UNAVAILABLE)
            self.assertEqual(window.badge.text(), "状态未知")
            self.assertNotIn("Node", window.notice.text())
            status = {"state": "running", "authenticated": True, "loginState": "idle",
                      "baseUrl": "http://127.0.0.1:4140/v1", "message": "", "deviceCode": None}
            window.completed("status", status)
            self.assertEqual(window.badge.text(), "运行中")
            self.assertEqual(window.detail.text(), "服务已就绪，可以发送请求。")
            self.assertEqual(window.notice.text(), "连接已恢复，当前服务状态已更新。")
            self.assertEqual(window.raw_notice, "")
            self.assertTrue(window.manager_connected)
            window.close()
            window.deleteLater()

    def test_lost_connection_hides_stale_state_and_keeps_unrelated_operation_errors(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Dispatcher, "request"):
            store = UserStore(Path(directory), Path(directory) / "user")
            window = MainWindow(store, testing=True)
            status = {"state": "running", "authenticated": True, "loginState": "idle",
                      "baseUrl": "http://127.0.0.1:4140/v1", "message": "", "deviceCode": None, "username": "test-user"}
            window.completed("status", status)
            window.failed("start", CONTROL_UNAVAILABLE)
            self.assertFalse(window.manager_connected)
            self.assertFalse(window.authenticated)
            self.assertEqual(window.badge.text(), "状态未知")
            self.assertIn("可能仍在运行", window.detail.text())
            self.assertNotIn("test-user", window.account.text())
            self.assertTrue(window.login_button.isHidden())
            self.assertTrue(window.logout_button.isHidden())
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.stop_button.isEnabled())
            self.assertFalse(window.listener_button.isEnabled())
            window.language_box.setCurrentIndex(window.language_box.findData("en"))
            self.assertEqual(window.badge.text(), "UNKNOWN")
            window.completed("status", status)
            self.assertTrue(window.manager_connected)
            self.assertTrue(window.stop_button.isEnabled())
            self.assertEqual(window.account.text(), "Signed in as test-user")
            self.assertEqual(window.notice.text(), "Connection restored. Service status is up to date.")
            window.failed("startup-disable", "Permission denied")
            window.completed("status", status)
            self.assertEqual(window.notice.text(), "Permission denied")
            window.close()
            window.deleteLater()
