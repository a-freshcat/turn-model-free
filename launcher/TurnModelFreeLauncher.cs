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
        string gatewayDir = FindGatewayDir(baseDir);
        if (gatewayDir == null)
        {
            return Fail("找不到 gateway 目录。\r\n\r\n" +
                        "这个 exe 应该和 launch-desktop.cmd 放在同一层，" +
                        "或者直接放在 gateway 目录里。\r\n当前目录：" + baseDir);
        }

        string script = Path.Combine(gatewayDir, "launch_desktop.py");
        if (!File.Exists(script))
        {
            return Fail("找不到启动脚本：\r\n" + script);
        }

        string pythonw = FindPythonw(gatewayDir);
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
        };

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

        child.WaitForExit();
        int code = child.ExitCode;
        child.Dispose();
        if (code == 0) return 0;

        string log = Path.Combine(gatewayDir, ".gateway-desktop.log");
        var message = new StringBuilder();
        message.Append("桌面控制台退出，代码 ").Append(code).Append("。\r\n\r\n");
        if (File.Exists(log))
        {
            message.Append("最近的诊断日志（").Append(log).Append("）：\r\n\r\n");
            message.Append(Tail(log, 1600));
            message.Append("\r\n\r\n");
        }
        message.Append("要看完整输出，请双击 launch-desktop.cmd（那个黑窗口是排障入口）。");
        return Fail(message.ToString());
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
    /// pythonw 优先本包自带的 .venv（分发目录里可能带着），其次环境变量，
    /// 最后才用 PATH 上的。都找不到就返回 null —— 让调用方说清楚怎么修。
    /// </summary>
    private static string FindPythonw(string gatewayDir)
    {
        string fromEnv = Environment.GetEnvironmentVariable("TMF_PYTHONW");
        if (!string.IsNullOrEmpty(fromEnv) && File.Exists(fromEnv)) return fromEnv;

        string[] local =
        {
            Path.Combine(gatewayDir, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(gatewayDir, "..", ".venv", "Scripts", "pythonw.exe"),
        };
        foreach (string candidate in local)
        {
            string full = Path.GetFullPath(candidate);
            if (File.Exists(full)) return full;
        }

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

    /// <summary>
    /// 失败出口。没有控制台，所以只能靠 MessageBox；自动化测试用
    /// TMF_LAUNCHER_NO_DIALOG=1 关掉它，只留退出码。
    /// </summary>
    private static int Fail(string message)
    {
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
