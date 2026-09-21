using System.Security.Cryptography;
using System.Text;

namespace EvoMesh.Desktop;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--self-test")
        {
            DesktopSelfTest.Run(args.Length > 1 ? args[1] : FindRepositoryRoot());
            return 0;
        }
        if (args.Length > 0 && args[0] == "--control-self-test")
        {
            var testRoot = args.Length > 1 ? Path.GetFullPath(args[1]) : FindRepositoryRoot();
            var testUv = args.Length > 2 ? args[2] : "uv";
            DesktopSelfTest.RunControlAsync(testRoot, testUv).GetAwaiter().GetResult();
            return 0;
        }
        if (args.Length > 0 && args[0] == "--health-recovery-self-test")
        {
            var testRoot = args.Length > 1 ? Path.GetFullPath(args[1]) : FindRepositoryRoot();
            var testUv = args.Length > 2 ? args[2] : "uv";
            DesktopSelfTest.RunHealthRecoveryAsync(testRoot, testUv).GetAwaiter().GetResult();
            return 0;
        }
        var root = args.Length > 0 ? Path.GetFullPath(args[0]) : FindRepositoryRoot();
        var uv = args.Length > 1 ? args[1] : "uv";
        if (!TryAcquireSingleInstance(root))
        {
            MessageBox.Show(
                "EvoMesh Control Center is already running for this checkout. Switch to "
                    + "the existing window instead of opening a second one.",
                "EvoMesh",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return 0;
        }
        InstallCrashLogging(root);
        ApplicationConfiguration.Initialize();
        Application.Run(new MainForm(root, uv));
        return 0;
    }

    // Kept alive for the life of the process -- an OS-level lock, same
    // reasoning as src/evomesh/singleton.py's SingletonLock for the mesh
    // process itself (found live: a second Control Center launched a second
    // mesh against the same data with nothing stopping the first). A crash
    // or kill releases a Mutex automatically when Windows closes its
    // handles, so there is no stale-lock file to clean up by hand. Scoped
    // to `root`, not the whole machine, so two Control Centers pointed at
    // two different checkouts remain a legitimate, separate setup -- only
    // a second one against the *same* checkout is refused.
    private static Mutex? _singleInstanceMutex;

    private static bool TryAcquireSingleInstance(string root)
    {
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(root.ToLowerInvariant()));
        var name = "Global\\EvoMesh.Desktop." + Convert.ToHexString(digest);
        _singleInstanceMutex = new Mutex(initiallyOwned: true, name, out var createdNew);
        return createdNew;
    }

    /// <summary>
    /// Without this, an exception that escapes the UI thread, a background
    /// health-loop callback, or an unobserved Task silently kills the whole
    /// Control Center process -- which also kills the only thing watching the
    /// mesh for its "bring me back up on new code" exit code. The mesh then
    /// keeps running orphaned until a human notices it stopped promoting
    /// generations and restarts the Control Center by hand. This turns that
    /// silent death into a line in control-center.log so the next one is
    /// diagnosable instead of a multi-hour mystery gap in the logs.
    /// </summary>
    private static void InstallCrashLogging(string root)
    {
        var logPath = Path.Combine(root, ".runtime", "logs", "control-center.log");

        void LogFatal(string source, object? exceptionObj)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
                File.AppendAllText(
                    logPath,
                    $"{DateTimeOffset.Now:O} [FATAL:{source}] {exceptionObj}{System.Environment.NewLine}",
                    System.Text.Encoding.UTF8);
            }
            catch
            {
                // The process is already going down for an unrelated reason;
                // a failure here must never mask or replace that reason.
            }
        }

        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) =>
            LogFatal("UIThread", e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            LogFatal(e.IsTerminating ? "AppDomain(terminating)" : "AppDomain", e.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            LogFatal("UnobservedTask", e.Exception);
            e.SetObserved();
        };
    }

    private static string FindRepositoryRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "pyproject.toml")))
            {
                return current.FullName;
            }
            current = current.Parent;
        }
        return Environment.CurrentDirectory;
    }
}
