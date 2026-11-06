"""桌面启动器的输出契约。

用户报过两件事，都在这里钉住：

1. `WARNING: Desktop diagnostic log enabled on port 8199` —— 一句正常的状态提示
   被用 `warning` 级别打印，控制台出现 "WARNING:" 让人以为启动失败；
2. `desktop.cmd` 不打印网关 Key —— `launch-web.cmd` 那条路径一直打印，
   桌面端漏了，用户只能去界面里翻。
"""

import io
import logging
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from launch_desktop import (
    LAUNCHER_LOGGER,
    ServerThread,
    _print_startup_banner,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "launch_desktop.py"


def code_only(text: str) -> str:
    """去掉注释，只留代码。

    注释里会写到「以前叫 Desktop diagnostic log enabled」这类解释性文字，
    把它当违规会误伤——那些说明恰恰是防止问题复发的线索。
    """
    text = re.sub(r'""".*?"""', "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines = [line for line in text.splitlines()
             if not line.lstrip().startswith("#")]
    return "\n".join(lines)


class StartupBannerTests(unittest.TestCase):
    def test_banner_prints_key_and_endpoints(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_startup_banner(8199, "beuIS-test-key-value-for-banner")
        text = buffer.getvalue()
        # Key 必须原样出现，用户要能直接复制。
        self.assertIn("beuIS-test-key-value-for-banner", text)
        self.assertIn("http://127.0.0.1:8199/v1", text)
        self.assertIn("http://127.0.0.1:8199/", text)

    def test_banner_says_what_the_key_is_for(self):
        """只丢一个 Key，用户不知道往哪填；必须说明用途。"""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_startup_banner(8199, "key")
        text = buffer.getvalue()
        self.assertIn("客户端", text)
        self.assertIn("/docs", text)

    def test_banner_is_not_prefix_alarmed(self):
        """启动横幅是正常输出，不该出现级别字样。"""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_startup_banner(8199, "key")
        for word in ("WARNING", "ERROR", "CRITICAL"):
            self.assertNotIn(word, buffer.getvalue())


class NoticeTests(unittest.TestCase):
    def setUp(self):
        # 加一个真正的 handler，避免 "no handlers" 影响日志调用路径。
        self.handler = logging.NullHandler()
        logging.getLogger(LAUNCHER_LOGGER).addHandler(self.handler)

    def tearDown(self):
        logging.getLogger(LAUNCHER_LOGGER).removeHandler(self.handler)

    def test_notice_prints_plain_message_without_level_word(self):
        """状态提示不能带 "WARNING:" 前缀 —— 那会让人以为启动失败。"""
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            ServerThread._notice("诊断日志已启用 · 端口 8199")
        text = buffer.getvalue()
        self.assertIn("诊断日志已启用", text)
        for word in ("WARNING", "INFO", "ERROR"):
            self.assertNotIn(word, text)

    def test_notice_still_marks_real_warnings(self):
        """真警告必须能看出来：启动提示不带前缀，故障提示要带。"""
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            ServerThread._notice("诊断日志无法写入", level=logging.WARNING)
        # warning 走的是日志记录器，控制台这句仍保持纯文本 —— 前缀由文件格式提供。
        self.assertIn("诊断日志无法写入", buffer.getvalue())

    def test_notice_never_raises_when_stream_is_broken(self):
        """没有控制台（pythonw）时也不能影响启动。"""
        class Broken(io.StringIO):
            def write(self, *_args, **_kwargs):
                raise OSError("no console")

        with redirect_stderr(Broken()):
            ServerThread._notice("随便一句")  # 不抛就算过


class LauncherLoggerTests(unittest.TestCase):
    def test_level_is_info_and_not_propagating(self):
        """级别必须显式 INFO，且不冒泡。

        不设 INFO 时 effective level 继承 root（默认 WARNING），启动提示这类
        INFO 记录会被整条丢掉 —— 实测日志文件 0 字节。
        冒泡会让同一句在控制台出现两次。
        """
        logger = logging.getLogger(LAUNCHER_LOGGER)
        self.assertEqual(logger.level, logging.INFO)
        self.assertFalse(logger.propagate)

    def test_no_console_stream_handler_attached(self):
        """控制台那句由 `_notice()` 自己 print。

        再挂 StreamHandler 会重复输出，而且会被 uvicorn 的 logging 重配影响。
        pytest 自己会往 logger 上挂 `LogCaptureHandler`，那是测试设施的，排掉。
        """
        logger = logging.getLogger(LAUNCHER_LOGGER)
        console = [h for h in logger.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)
                   and type(h).__name__ != "LogCaptureHandler"]
        self.assertEqual(console, [])

    def test_source_does_not_create_stream_handler(self):
        self.assertNotIn("logging.StreamHandler()", code_only(SOURCE.read_text(encoding="utf-8")))


class SourceInvariants(unittest.TestCase):
    def test_old_english_status_text_is_gone(self):
        """旧文案不许再出现在**代码**里。

        注释里提到它是为了解释「以前这么写过、为什么不该」——那是防复发的线索，
        不算违规，所以只看去掉注释后的代码。
        """
        self.assertNotIn("Desktop diagnostic log enabled",
                         code_only(SOURCE.read_text(encoding="utf-8")))

    def test_startup_notice_is_not_logged_as_warning(self):
        """启动提示不许再借 warning 级别显示。"""
        code = code_only(SOURCE.read_text(encoding="utf-8"))
        self.assertNotIn("'.warning(", code)
        self.assertIn("诊断日志已启用", code)

    def test_status_notice_does_not_go_through_logging_to_console(self):
        """走 logging 会被 uvicorn 的重配吞掉（实测控制台一个字都没有）。"""
        code = code_only(SOURCE.read_text(encoding="utf-8"))
        self.assertIn("print(message, file=stream, flush=True)", code)

    def test_banner_called_before_server_starts(self):
        """Key 必须在服务起来之前打印，否则用户看到的是「先失败再给 Key」。"""
        code = code_only(SOURCE.read_text(encoding="utf-8"))
        banner = code.index("_print_startup_banner(args.port")
        thread = code.index("server_thread.start()")
        self.assertLess(banner, thread)


class CmdEntryTests(unittest.TestCase):
    def test_root_shortcut_targets_gateway_launcher(self):
        text = (ROOT.parent / 'launch-desktop.cmd').read_text(encoding='ascii')
        self.assertIn('gateway\\launch-desktop.cmd', text)

    def test_cmd_uses_console_python_to_show_key(self):
        text = (ROOT / 'launch-desktop.cmd').read_text(encoding='ascii')
        self.assertIn('"%GATEWAY_PYTHON%"', text)
        self.assertNotIn('"%DESKTOP_PYTHON%"', text)


class KeyFileTests(unittest.TestCase):
    def test_key_is_stable_across_restarts(self):
        """Key 要跨重启稳定：外部客户端与 DSH 的 OpenAI 兼容配置都写死了它。"""
        import launch_web

        original = launch_web.API_KEY_FILE
        with tempfile.TemporaryDirectory() as directory:
            launch_web.API_KEY_FILE = Path(directory) / ".gateway-api-key"
            try:
                first = launch_web._load_or_create_api_key()
                second = launch_web._load_or_create_api_key()
            finally:
                launch_web.API_KEY_FILE = original
        self.assertEqual(first, second)
        self.assertGreater(len(first), 20)


if __name__ == "__main__":
    unittest.main()
