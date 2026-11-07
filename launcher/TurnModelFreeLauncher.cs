// turn-model-free 免控制台启动器。
//
// 为什么需要它：`launch-desktop.cmd` 会留一个黑窗口。那个窗口有价值（打印网关
// 地址与 Key），但用户要的是「像 app 那样双击就开」。所以这里用 `winexe`
// （Windows 子系统，没有控制台）做一层薄壳：
//
//   1. 用 `pythonw.exe` 静默拉起 `gateway\launch_desktop.py`（它内部起 pywebview
//      窗口并跑 uvicorn，窗口关掉时进程一起退出）；
//   2. 子进程的退出码原样返回；
//   3. 失败才弹一个 MessageBox —— 没有控制台时这是唯一的报错通道，
//      同时指路「用 launch-desktop.cmd 看详细输出」。
//
// 不做的事：不自己起服务、不解析网关参数、不打印 Key。Key 在界面「接入设置」
// 里可复制，也在 `gateway\.gateway-api-key`。少一份逻辑就少一处会和 Python 侧
// 走偏的地方。
//
// 编译（见 scripts/build-launcher.ps1）：
//   csc.exe /target:winexe /platform:anycpu /optimize+ /win32icon:launcher\app.ico
//           /out:TurnModelFree.exe launcher\TurnModelFreeLauncher.cs

using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class TurnModelFreeLauncher
{
    private const int DefaultPort = 8199;
    // CREATE_NO_WINDOW：即使将来换回 python.exe（控制台子系统）也不弹黑框。
    private const int CreateNoWindow = 0x08000000;

    [STAThread]
    private static int Main(string[] args)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        Trace("base=" + baseDir);
        string gatewayDir = FindGatewayDir(baseDir);
        Trace("gateway=" + (gatewayDir ?? "(null)"));
        if (gatewayDir == null)
        {
            return Fail("找不到 gateway 目录。\r\n\r\n" +
                        "这个 exe 应该和 launch-desktop.cmd 放在同一层，" +
                        "或者直接放在 gateway 目录里。\r\n当前目录：" + baseDir);
        }

        string script = Path.Combine(gatewayDir, "launch_desktop.py");
        Trace("script=" + script + " exists=" + File.Exists(script));
        if (!File.Exists(script))
        {
            return Fail("找不到启动脚本：\r\n" + script);
        }

        string pythonw = FindPythonw(gatewayDir);
        Trace("pythonw=" + (pythonw ?? "(null)"));
        if (pythonw == null)
        {
            return Fail("没有找到 pythonw.exe。\r\n\r\n" +
                        "装好 Python 后重试，或把解释器路径写进环境变量 TMF_PYTHONW。\r\n" +
                        "要用命令行看详细输出，请改双击 launch-desktop.cmd。");
        }

        string port = PortOf(args, DefaultPort);
        var info = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = Quote(script) + " --port " + port + Passthrough(args),
            WorkingDirectory = gatewayDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            // 抓 stderr：解释器起不来（缺依赖、脚本报错）时，Python 把原因写在这里，
            // 而 `.gateway-desktop.log` 只有**跑起来之后**才会写。不带这两行，用户看到
            // 的是一个 20 分钟前的旧日志 + 「退出代码 1」，等于什么都没说。
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        };

        // 日志的修改时间要在启动**之前**记下来：之后用它判断这次有没有真的写日志。
        string log = Path.Combine(gatewayDir, ".gateway-desktop.log");
        DateTime logStamp = File.Exists(log) ? File.GetLastWriteTimeUtc(log) : DateTime.MinValue;

        Process child;
        try
        {
            child = Process.Start(info);
        }
        catch (Exception error)
        {
            return Fail("启动失败：" + error.Message);
        }
        if (child == null)
        {
            return Fail("启动失败：进程没有起来。");
        }

        var captured = new StringBuilder();
        child.ErrorDataReceived += (sender, e) => { if (e.Data != null) lock (captured) captured.AppendLine(e.Data); };
        child.OutputDataReceived += (sender, e) => { if (e.Data != null) lock (captured) captured.AppendLine(e.Data); };
        child.BeginErrorReadLine();
        child.BeginOutputReadLine();

        child.WaitForExit();
        int code = child.ExitCode;
        child.Dispose();
        if (code == 0) return 0;

        var message = new StringBuilder();
        message.Append("桌面控制台退出，代码 ").Append(code).Append("。\r\n\r\n");

        // 解释器根本没加载起来时 stderr 就是全部真相，优先展示它。
        string stderr;
        lock (captured) stderr = captured.ToString().Trim();
        if (stderr.Length > 0)
        {
            message.Append("子进程输出：\r\n\r\n");
            message.Append(Trim(stderr, 1400));
            message.Append("\r\n\r\n");
        }

        bool fresh = File.Exists(log) && File.GetLastWriteTimeUtc(log) > logStamp;
        if (fresh)
        {
            message.Append("最近的诊断日志（").Append(log).Append("）：\r\n\r\n");
            message.Append(Tail(log, 1200));
            message.Append("\r\n\r\n");
        }
        else if (stderr.Length == 0)
        {
            // 既没有新日志也没有 stderr：最典型就是「pythonw 不是这份安装的」。
            message.Append("这次启动没有产生新的诊断日志，通常说明 Python 环境不对：\r\n")
                   .Append("实际使用的解释器：").Append(pythonw).Append("\r\n\r\n");
        }

        if (LooksLikeMissingDependency(stderr))
        {
            message.Append("这看起来是缺依赖。装上依赖再试：\r\n\r\n")
                   .Append("  cd \"").Append(gatewayDir).Append("\"\r\n")
                   .Append("  python -m venv .venv\r\n")
                   .Append("  .venv\\Scripts\\python.exe -m pip install -r requirements.txt\r\n\r\n");
        }

        message.Append("要看完整输出，请双击 launch-desktop.cmd（那个黑窗口是排障入口）。");
        return Fail(message.ToString());
    }

    /// <summary>把 traceback 里最常见的「模块不存在」认出来，好给出可执行的修法。</summary>
    private static bool LooksLikeMissingDependency(string stderr)
    {
        if (string.IsNullOrEmpty(stderr)) return false;
        return stderr.IndexOf("ModuleNotFoundError", StringComparison.Ordinal) >= 0
            || stderr.IndexOf("ImportError", StringComparison.Ordinal) >= 0
            || stderr.IndexOf("No module named", StringComparison.Ordinal) >= 0;
    }

    private static string Trim(string text, int limit)
    {
        if (text.Length <= limit) return text;
        return "…" + text.Substring(text.Length - limit);
    }

    /// <summary>
    /// exe 的三种落法都要能开：
    ///   · 发布包根目录（<c>TurnModelFree.exe</c> 与 <c>gateway\</c> 同级）；
    ///   · <c>launcher\</c> 里（本仓库的构建产物位置，上一级才是包根）；
    ///   · 直接被拖进 <c>gateway\</c> 里。
    /// </summary>
    private static string FindGatewayDir(string baseDir)
    {
        string[] candidates =
        {
            Path.Combine(baseDir, "gateway"),
            baseDir,
            Path.GetFullPath(Path.Combine(baseDir, "..", "gateway")),
            Path.GetFullPath(Path.Combine(baseDir, "..")),
        };
        foreach (string candidate in candidates)
        {
            if (File.Exists(Path.Combine(candidate, "launch_desktop.py"))) return candidate;
        }
        return null;
    }

    /// <summary>
    /// pythonw 的查找顺序：本包自带 .venv → 同级的 .venv → TMF_PYTHONW → PATH → pyw.exe。
    ///
    /// `.venv` 必须排在 PATH 前面：PATH 上那个是不带本项目依赖的通用解释器，用它
    /// 启动只会得到「退出代码 1 + 一段旧日志」。仓库里编译出来的 exe 旁边没有
    /// `.venv`，正好会踩这个坑，所以顺序和提示都要写清楚。
    /// </summary>
    private static string FindPythonw(string gatewayDir)
    {
        string[] local =
        {
            Path.Combine(gatewayDir, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(gatewayDir, "..", ".venv", "Scripts", "pythonw.exe"),
        };
        foreach (string candidate in local)
        {
            string full;
            try { full = Path.GetFullPath(candidate); }
            catch (Exception) { continue; }
            if (File.Exists(full)) return full;
        }

        string fromEnv = Environment.GetEnvironmentVariable("TMF_PYTHONW");
        if (!string.IsNullOrEmpty(fromEnv) && File.Exists(fromEnv)) return fromEnv;

        string onPath = SearchPath("pythonw.exe");
        if (onPath != null) return onPath;
        // py launcher 的窗口版：`pyw.exe -3`，但这里只需要可执行文件，
        // 参数交给下面 Arguments 拼装（默认解释器）。
        string pyw = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "pyw.exe");
        return File.Exists(pyw) ? pyw : null;
    }

    private static string SearchPath(string name)
    {
        string path = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (string part in path.Split(Path.PathSeparator))
        {
            if (part.Length == 0) continue;
            try
            {
                string candidate = Path.Combine(part.Trim('"'), name);
                if (File.Exists(candidate)) return candidate;
            }
            catch (ArgumentException)
            {
                // PATH 里有非法字符的条目，跳过即可。
            }
        }
        return null;
    }

    /// <summary>取 --port 的值（缺省 8199），只用于拼参数和提示。</summary>
    private static string PortOf(string[] args, int fallback)
    {
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--port" && i + 1 < args.Length) return args[i + 1];
            if (args[i].StartsWith("--port=", StringComparison.Ordinal))
                return args[i].Substring("--port=".Length);
        }
        return fallback.ToString();
    }

    /// <summary>把除 --port 之外的参数原样转给 Python（--real 之类）。</summary>
    private static string Passthrough(string[] args)
    {
        var builder = new StringBuilder();
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--port") { i++; continue; }
            if (args[i].StartsWith("--port=", StringComparison.Ordinal)) continue;
            builder.Append(' ').Append(Quote(args[i]));
        }
        return builder.ToString();
    }

    private static string Quote(string value)
    {
        if (value.Length == 0) return "\"\"";
        if (value.IndexOf(' ') < 0 && value.IndexOf('"') < 0) return value;
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static string Tail(string path, int limit)
    {
        try
        {
            string text = File.ReadAllText(path);
            if (text.Length <= limit) return text;
            return "…" + text.Substring(text.Length - limit);
        }
        catch (IOException)
        {
            return "（日志读取失败）";
        }
        catch (UnauthorizedAccessException)
        {
            return "（日志读取失败）";
        }
    }

    /// <summary>把启动过程写进 TMF_LAUNCHER_TRACE 指定的文件（没有该变量就什么都不做）。</summary>
    private static void Trace(string line)
    {
        string path = Environment.GetEnvironmentVariable("TMF_LAUNCHER_TRACE");
        if (string.IsNullOrEmpty(path)) return;
        try
        {
            File.AppendAllText(path, DateTime.Now.ToString("HH:mm:ss.fff") + " " + line + "\r\n",
                               new UTF8Encoding(false));
        }
        catch (Exception) { }
    }

    /// <summary>
    /// 失败出口。没有控制台，所以只能靠 MessageBox；自动化测试用
    /// TMF_LAUNCHER_NO_DIALOG=1 关掉它，只留退出码。
    ///
    /// `TMF_LAUNCHER_REPORT=<path>` 会把同一段文案写进文件：弹窗在自动化里读不到，
    /// 而「报错说了什么」恰恰是最该被测试锁住的部分（曾经它只显示一份 20 分钟前
    /// 的旧日志）。
    /// </summary>
    private static int Fail(string message)
    {
        Trace("FAIL: " + message.Replace("\r\n", " / "));
        string report = Environment.GetEnvironmentVariable("TMF_LAUNCHER_REPORT");
        if (!string.IsNullOrEmpty(report))
        {
            try { File.WriteAllText(report, message, new UTF8Encoding(false)); }
            catch (Exception) { /* 写不进去不影响退出码 */ }
        }
        if (Environment.GetEnvironmentVariable("TMF_LAUNCHER_NO_DIALOG") != "1")
        {
            try
            {
                MessageBox.Show(message, "turn-model-free",
                                MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            catch (Exception)
            {
                // 无桌面会话（服务/计划任务）时弹不出来，仍然用退出码表达失败。
            }
        }
        return 1;
    }
}
