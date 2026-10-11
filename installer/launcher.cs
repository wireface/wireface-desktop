// Wireface.exe: starts Wireface Desktop from its install folder, as Wireface.cmd does from a clone. Built by
// scripts\build-installer.ps1 with the C# compiler that comes with Windows (.NET Framework 4).
//
// The install folder holds python\ (Python with the app's packages), wireface\ web\ shell\ (the app) and deps\
// (Electron, MediaPipe, and the speech models once downloaded). The first run downloads the speech models in a
// console window; a failure there isn't fatal: the face then runs with captions only and says what is missing.
// Arguments are passed on: Wireface.exe --face work.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string python = Path.Combine(root, @"python\python.exe");
        string pythonw = Path.Combine(root, @"python\pythonw.exe");
        if (!File.Exists(pythonw) || !Directory.Exists(Path.Combine(root, "wireface")))
        {
            MessageBox.Show("Some of Wireface's files are missing. Please install it again.", "Wireface",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        try
        {
            if (Run(python, "-m wireface.models --check", root, false) != 0)
            {
                string install = Quote(python) + " -m wireface.models --install";
                Run("cmd.exe", "/s /c \"title Wireface: downloading the speech models (once) & " + install +
                    " || pause\"", root, true);
            }
            var start = new ProcessStartInfo(pythonw, "-m wireface " + Join(args))
            {
                WorkingDirectory = root,
                UseShellExecute = false,
            };
            Process.Start(start);
            return 0;
        }
        catch (Exception e)
        {
            MessageBox.Show("Wireface couldn't start: " + e.Message, "Wireface", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    /// <summary>Run a program and wait for it: hidden, or in a console window of its own.</summary>
    static int Run(string exe, string arguments, string cwd, bool visible)
    {
        var info = new ProcessStartInfo(exe, arguments)
        {
            WorkingDirectory = cwd,
            UseShellExecute = visible,
            CreateNoWindow = !visible,
        };
        using (var p = Process.Start(info))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }

    static string Join(string[] args)
    {
        var parts = new string[args.Length];
        for (int i = 0; i < args.Length; i++) parts[i] = Quote(args[i]);
        return string.Join(" ", parts);
    }

    /// <summary>One command-line argument, quoted the way the C runtime reads it back.</summary>
    static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return arg;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\') { slashes++; continue; }
            sb.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
            slashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', slashes * 2);
        return sb.Append('"').ToString();
    }
}
