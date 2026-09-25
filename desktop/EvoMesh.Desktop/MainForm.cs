using System.Diagnostics;
using System.Drawing;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace EvoMesh.Desktop;

internal sealed class MainForm : Form
{
    private static readonly (string Id, string Name)[] CoreAgents =
    [
        ("architect", "Agent Architect"),
        ("guardian", "Guardian"),
        ("evaluator", "Evaluator"),
        ("evolver", "Environment Evolver"),
    ];
    private readonly EvoMeshRuntimeProcess _runtime;
    private readonly string _configPath;
    private readonly string _secretsPath;
    private EvoMeshSecretsSettings _secrets = new();
    private readonly NotifyIcon _trayIcon;
    private bool _exitRequested;
    private readonly RichTextBox _output = new();
    private readonly TextBox _command = new();
    private readonly Label _status = new();
    private readonly Button _start = new();
    private readonly Button _stop = new();
    private readonly Dictionary<string, (TextBox Url, ComboBox Model, TextBox Key, TextBox NumCtx, ComboBox KeyRef)> _providers = [];
    private readonly Dictionary<string, (ComboBox Provider, ComboBox Model, TextBox NumCtx)> _systemAgents = [];
    private bool _loadingSettings;
    private bool _running;
    private DateTimeOffset? _lastCheck;
    private TextBox _environmentName = null!;
    private TextBox _dataPath = null!;
    private TextBox _generationPath = null!;
    private ComboBox _logLevel = null!;
    private ComboBox _defaultProvider = null!;
    private Button _saveSettings = null!;
    private Button _reloadSettings = null!;
    private Label _settingsNotice = null!;
    private ComboBox _agentProvider = null!;
    private ComboBox _agentModel = null!;
    private TabControl _tabs = null!;
    private readonly ListView _agentList = new();
    private readonly System.Windows.Forms.Timer _agentRefreshTimer = new() { Interval = 4000 };
    // Each agent keeps its own scrollback here -- switching the selected row
    // and back must not lose what was already said to it, and nothing here
    // should ever bleed into another agent's panel or the shared Console tab.
    private readonly Dictionary<string, List<string>> _agentChatHistory = [];
    // Character ranges in _agentChatOutput that a FILE: line rendered as a
    // clickable link, and the absolute path each one opens -- rebuilt
    // whenever the output is cleared (SelectAgent), since the ranges are
    // only meaningful for whatever is currently on screen.
    private readonly List<(int Start, int Length, string Path)> _agentChatLinks = [];
    private List<AgentRow> _agentRows = [];
    private AgentRow? _selectedAgent;
    private Panel _agentPlaceholder = null!;
    private Panel _agentDetail = null!;
    private Label _agentDetailName = null!;
    private Label _agentDetailStatus = null!;
    private Label _agentDetailMeta = null!;
    private RichTextBox _agentChatOutput = null!;
    private TextBox _agentChatInput = null!;
    private Button _agentStartStop = null!;
    private Button _agentMuteToggle = null!;
    private Button _agentDeleteButton = null!;
    private TextBox _agentNumCtxField = null!;
    private TextBox _agentTelegramToken = null!;
    private Label _agentTelegramStatus = null!;
    private TextBox _newAgentRequest = null!;
    private TextBox _runtimeCycleSeconds = null!;
    private TextBox _evolutionCycleSeconds = null!;
    private CheckBox _autoPromote = null!;
    private CheckBox _autoRestart = null!;
    private TextBox _gitAuthorName = null!;
    private TextBox _gitAuthorEmail = null!;
    private CheckBox _gitAutoPush = null!;
    private TextBox _gitRemote = null!;
    private TextBox _gitBranch = null!;
    private CheckBox _telegramEnabled = null!;
    private TextBox _telegramToken = null!;
    private TextBox _telegramChats = null!;
    private CheckBox _telegramAdoptFirst = null!;
    private CheckBox _telegramAnnouncements = null!;
    private Label _telegramNotice = null!;

    public MainForm(string rootPath, string uvExecutable)
    {
        _runtime = new EvoMeshRuntimeProcess(rootPath, uvExecutable);
        _configPath = Path.Combine(rootPath, "evomesh.yaml");
        _secretsPath = Path.Combine(rootPath, "evomesh.secrets.yaml");
        Text = "EvoMesh Control Center";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        MinimumSize = new Size(980, 680);
        Size = new Size(1200, 820);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10F);
        BackColor = Color.FromArgb(245, 248, 252);

        Controls.Add(BuildTabs());
        Controls.Add(BuildHeader());
        _runtime.OutputReceived += AppendOutput;
        _runtime.RunningChanged += UpdateRuntimeState;
        _runtime.HealthChecked += ShowHealthCheck;
        _agentRefreshTimer.Tick += async (_, _) => await RefreshAgentListAsync();
        _agentRefreshTimer.Start();
        EnsureConfiguration();
        LoadSettings();
        UpdateRuntimeState(false);

        // The mesh's own "land a generation, restart into it" cycle only
        // works while something is watching for its exit code -- that watcher
        // is this process. The [X] button used to close the window outright,
        // which ends the whole Control Center (Application.Run returns) and
        // silently abandons the mesh: it keeps running orphaned until it next
        // asks to restart, at which point nobody is left to bring it back.
        // Minimizing to the tray instead keeps that watcher alive; a real
        // exit is only ever the tray menu's "Exit" item.
        _trayIcon = new NotifyIcon
        {
            Icon = Icon,
            Text = "EvoMesh Control Center",
            Visible = false,
            ContextMenuStrip = BuildTrayMenu(),
        };
        _trayIcon.DoubleClick += (_, _) => RestoreFromTray();

        Shown += async (_, _) =>
        {
            if (await _runtime.TryAttachAsync())
            {
                AppendOutput("[Control Center connected automatically]");
            }
            // From here the mesh is watched continuously. A mesh started from
            // the launcher script, or one that restarted itself into a new
            // generation, is picked up without anyone touching this window.
            _runtime.StartHealthLoop();
            await RefreshOllamaModelsAsync(showErrors: false);
        };
    }

    private ContextMenuStrip BuildTrayMenu()
    {
        var menu = new ContextMenuStrip();
        menu.Items.Add("Open Control Center", null, (_, _) => RestoreFromTray());
        menu.Items.Add(new ToolStripSeparator());
        // Closing this dashboard was never actually what stopped the mesh --
        // Dispose() only ever drops the control connection, never sends
        // /exit -- so a run-supervised.ps1-style setup (the mesh supervised
        // by its own process, not spawned by this one) keeps right on
        // running either way. The old label said "stops the mesh" anyway,
        // which taught a human to leave this window open forever out of
        // caution for something that was never true.
        menu.Items.Add("Exit Control Center (mesh keeps running)", null, (_, _) =>
        {
            _exitRequested = true;
            Close();
        });
        menu.Items.Add("Stop mesh and exit", null, async (_, _) =>
        {
            await RunSafeAsync(_runtime.StopAsync);
            _exitRequested = true;
            Close();
        });
        return menu;
    }

    private void RestoreFromTray()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
        _trayIcon.Visible = false;
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!_exitRequested && e.CloseReason == CloseReason.UserClosing && _runtime.IsRunning)
        {
            e.Cancel = true;
            Hide();
            _trayIcon.Visible = true;
            _trayIcon.ShowBalloonTip(
                4000,
                "EvoMesh is still running",
                "The mesh keeps evolving in the background -- closing this window (or exiting from " +
                "the tray icon) never stops it. Use \"Stop mesh and exit\" in the tray menu if you " +
                "actually want to stop it too.",
                ToolTipIcon.Info);
            return;
        }
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _trayIcon.Dispose();
            _agentRefreshTimer.Dispose();
            _runtime.Dispose();
        }
        base.Dispose(disposing);
    }

    internal void ValidateUiForTest()
    {
        CreateControl();
        PerformLayout();
        var header = Controls.OfType<Panel>().Single(control => control.Dock == DockStyle.Top);
        var layout = header.Controls.OfType<TableLayoutPanel>().Single();
        header.PerformLayout();
        layout.PerformLayout();
        if (_status.Bounds.IntersectsWith(_start.Bounds))
        {
            throw new InvalidOperationException("Status label overlaps the Start button.");
        }
        if (!_providers.TryGetValue("ollama", out var ollama) ||
            ollama.Model.DropDownStyle != ComboBoxStyle.DropDown)
        {
            throw new InvalidOperationException("Settings Ollama model must be an editable dropdown.");
        }
        if (_systemAgents.Count != CoreAgents.Length ||
            _systemAgents.Values.Any(item => item.Model.DropDownStyle != ComboBoxStyle.DropDown))
        {
            throw new InvalidOperationException("All core agents must have editable model dropdowns.");
        }
    }

    private Control BuildHeader()
    {
        var headerColor = Color.FromArgb(8, 42, 82);
        var panel = new Panel { Dock = DockStyle.Top, Height = 82, BackColor = headerColor };
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 4,
            RowCount = 1,
            Padding = new Padding(18, 10, 18, 10),
            BackColor = headerColor,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 235));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));

        var brand = new Panel { Dock = DockStyle.Fill, BackColor = headerColor };
        var title = new Label { Text = "EvoMesh", ForeColor = Color.White, Font = new Font("Segoe UI", 21F, FontStyle.Bold), AutoSize = true, Location = new Point(0, 0) };
        var subtitle = new Label { Text = "Local multi-agent control center", ForeColor = Color.FromArgb(170, 220, 235), AutoSize = true, Location = new Point(3, 38) };
        brand.Controls.AddRange([title, subtitle]);

        _status.AutoSize = false;
        _status.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _status.Dock = DockStyle.Fill;
        _status.TextAlign = ContentAlignment.MiddleRight;
        _status.Margin = new Padding(4, 0, 12, 0);
        _start.Text = "Start Mesh";
        _start.Dock = DockStyle.Fill;
        _start.Margin = new Padding(5, 11, 5, 11);
        StyleButton(_start);
        _start.Click += async (_, _) => await RunSafeAsync(_runtime.StartAsync);
        _stop.Text = "Stop Mesh";
        _stop.Dock = DockStyle.Fill;
        _stop.Margin = new Padding(5, 11, 0, 11);
        StyleButton(_stop);
        _stop.Click += async (_, _) => await RunSafeAsync(_runtime.StopAsync);
        layout.Controls.Add(brand, 0, 0);
        layout.Controls.Add(_status, 1, 0);
        layout.Controls.Add(_start, 2, 0);
        layout.Controls.Add(_stop, 3, 0);
        panel.Controls.Add(layout);
        return panel;
    }

    private Control BuildTabs()
    {
        var tabs = new TabControl { Dock = DockStyle.Fill, Padding = new Point(18, 7) };
        tabs.TabPages.Add(BuildConsoleTab());
        tabs.TabPages.Add(BuildAgentsTab());
        tabs.TabPages.Add(BuildSettingsTab());
        // A jump straight to a current picture beats waiting up to
        // _agentRefreshTimer's own 4s tick the moment a human lands here.
        tabs.SelectedIndexChanged += async (_, _) =>
        {
            if (tabs.SelectedTab?.Text == "Agents")
            {
                await RefreshAgentListAsync();
            }
        };
        _tabs = tabs;
        return tabs;
    }

    private TabPage BuildConsoleTab()
    {
        var page = new TabPage("Console & Chat") { Padding = new Padding(14), BackColor = BackColor };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1 };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 92));

        var quick = new FlowLayoutPanel { Dock = DockStyle.Fill, Height = 45, AutoSize = true };
        foreach (var (text, command) in new[]
        {
            ("Status", "/status"), ("Agents", "/agents"), ("Ollama models", "/models ollama"),
            ("Skills", "/skills"), ("Evolution", "/evolution status"),
            ("Improvements", "/improvements"),
            ("World", "/context world"), ("Restart mesh", "/restart"), ("Help", "/help")
        })
        {
            var button = MakeButton(text, 120);
            button.Click += async (_, _) => await SendCommandAsync(command);
            quick.Controls.Add(button);
        }

        _output.Dock = DockStyle.Fill;
        _output.ReadOnly = true;
        _output.BackColor = Color.FromArgb(17, 25, 39);
        _output.ForeColor = Color.FromArgb(225, 235, 245);
        _output.Font = new Font("Cascadia Mono", 10F);
        _output.BorderStyle = BorderStyle.FixedSingle;

        var input = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(0, 10, 0, 0) };
        input.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        input.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 135));
        _command.Dock = DockStyle.Fill;
        _command.Multiline = true;
        _command.PlaceholderText = "Write a message to the selected agent or enter a /command...";
        _command.KeyDown += async (_, args) =>
        {
            if (args.KeyCode == Keys.Enter && !args.Shift)
            {
                args.SuppressKeyPress = true;
                await SendCurrentAsync();
            }
        };
        var send = MakeButton("Send", 120);
        send.Dock = DockStyle.Fill;
        send.Margin = new Padding(10, 0, 0, 0);
        send.Click += async (_, _) => await SendCurrentAsync();
        input.Controls.Add(_command, 0, 0);
        input.Controls.Add(send, 1, 0);

        layout.Controls.Add(quick, 0, 0);
        layout.Controls.Add(_output, 0, 1);
        layout.Controls.Add(input, 0, 2);
        page.Controls.Add(layout);
        return page;
    }

    private TabPage BuildAgentsTab()
    {
        var page = new TabPage("Agents") { Padding = new Padding(14), BackColor = BackColor };
        var outer = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        outer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        outer.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var newAgent = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            ColumnCount = 3,
            AutoSize = true,
            Padding = new Padding(0, 0, 0, 12),
        };
        newAgent.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        newAgent.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        newAgent.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        newAgent.Controls.Add(
            new Label { Text = "New agent:", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 10, 8, 3) },
            0, 0);
        _newAgentRequest = new TextBox
        {
            Dock = DockStyle.Fill,
            Margin = new Padding(3, 6, 8, 3),
            PlaceholderText = "Describe it, e.g. \"a Bulgarian research agent that reads D:\\Papers and uses qwen3:14b\" -- Architect asks the rest below.",
        };
        newAgent.Controls.Add(_newAgentRequest, 1, 0);
        var ask = MakeButton("Ask Architect", 150);
        ask.Margin = new Padding(0, 6, 0, 3);
        ask.Click += async (_, _) =>
        {
            if (string.IsNullOrWhiteSpace(_newAgentRequest.Text)) return;
            var text = _newAgentRequest.Text.Trim();
            _newAgentRequest.Clear();
            SelectAgentRowById("architect");
            await SendSelectedAgentChatAsync(text);
        };
        newAgent.Controls.Add(ask, 2, 0);

        var split = new SplitContainer
        {
            // A freshly constructed SplitContainer is not parented or Dock-sized
            // yet, so it validates Panel1MinSize/Panel2MinSize/SplitterDistance
            // against its own tiny default Width right here in the initializer --
            // a generous explicit Width first is what keeps that validation from
            // throwing before Dock=Fill ever gets a chance to take over.
            Width = 960,
            Height = 560,
            Dock = DockStyle.Fill,
            Orientation = Orientation.Vertical,
            SplitterWidth = 6,
            BackColor = Color.FromArgb(225, 230, 236),
        };
        split.Panel1MinSize = 240;
        split.Panel2MinSize = 380;
        split.SplitterDistance = 280;

        _agentList.View = View.Details;
        _agentList.FullRowSelect = true;
        _agentList.GridLines = false;
        _agentList.HideSelection = false;
        _agentList.MultiSelect = false;
        _agentList.Dock = DockStyle.Fill;
        _agentList.Font = new Font("Segoe UI", 9.5F);
        _agentList.Columns.Add("Agent", 160);
        _agentList.Columns.Add("Status", 100);
        _agentList.Columns.Add("Model", 220);
        _agentList.SelectedIndexChanged += (_, _) =>
        {
            var row = _agentList.SelectedItems.Count > 0 ? _agentList.SelectedItems[0].Tag as AgentRow : null;
            SelectAgent(row);
        };
        split.Panel1.Padding = new Padding(0, 0, 6, 0);
        split.Panel1.Controls.Add(_agentList);
        split.Panel2.Padding = new Padding(6, 0, 0, 0);
        split.Panel2.Controls.Add(BuildAgentDetailPanel());

        outer.Controls.Add(newAgent, 0, 0);
        outer.Controls.Add(split, 0, 1);
        page.Controls.Add(outer);
        return page;
    }

    /// <summary>
    /// The right half of the Agents tab: nothing selected shows a placeholder,
    /// a selection shows status, start/stop/mute/delete, the model/num_ctx
    /// editor, and a chat panel scoped to that one agent alone.
    /// </summary>
    private Control BuildAgentDetailPanel()
    {
        var container = new Panel { Dock = DockStyle.Fill };

        _agentPlaceholder = new Panel { Dock = DockStyle.Fill };
        _agentPlaceholder.Controls.Add(new Label
        {
            Text = "Select an agent on the left -- or create one above -- to chat with it, " +
                   "change its model, or delete it.",
            AutoSize = true,
            MaximumSize = new Size(360, 0),
            ForeColor = Color.DimGray,
            Location = new Point(4, 4),
        });

        _agentDetail = new Panel { Dock = DockStyle.Fill, Visible = false };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5 };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var header = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1 };
        _agentDetailName = new Label { AutoSize = true, Font = new Font("Segoe UI", 14F, FontStyle.Bold) };
        _agentDetailStatus = new Label { AutoSize = true, Margin = new Padding(0, 2, 0, 0) };
        _agentDetailMeta = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(520, 0),
            ForeColor = Color.DimGray,
            Margin = new Padding(0, 4, 0, 0),
        };
        header.Controls.Add(_agentDetailName, 0, 0);
        header.Controls.Add(_agentDetailStatus, 0, 1);
        header.Controls.Add(_agentDetailMeta, 0, 2);

        var actions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Margin = new Padding(0, 10, 0, 8) };
        _agentStartStop = MakeButton("Stop", 90);
        _agentStartStop.Click += async (_, _) => await ToggleSelectedAgentRunningAsync();
        _agentMuteToggle = MakeButton("Mute", 90);
        _agentMuteToggle.Click += async (_, _) => await ToggleSelectedAgentMutedAsync();
        var cycleNow = MakeButton("Cycle now", 100);
        cycleNow.Click += async (_, _) =>
        {
            if (_selectedAgent is { } row) await SendCommandAsync($"/cycle {Quote(row.Name)}");
        };
        _agentDeleteButton = MakeButton("Delete...", 100);
        _agentDeleteButton.FlatAppearance.BorderColor = Color.FromArgb(178, 34, 34);
        _agentDeleteButton.Click += async (_, _) => await DeleteSelectedAgentAsync();
        // What the cognitive runtime keeps per agent: its goals, the plans it
        // learned to reuse without a planning call, and its own rules.
        var inspect = new[] { ("Goals", "/goals"), ("Procedures", "/procedures"), ("Rules", "/rules") }
            .Select(item =>
            {
                var button = MakeButton(item.Item1, 100);
                button.Click += async (_, _) =>
                {
                    if (_selectedAgent is { } row) await SendCommandAsync($"{item.Item2} {Quote(row.Name)}");
                };
                return (Control)button;
            })
            .ToArray();
        actions.Controls.AddRange([_agentStartStop, _agentMuteToggle, cycleNow, .. inspect, _agentDeleteButton]);

        var manage = new GroupBox { Text = "Model", Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(12) };
        var mgrid = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 4, AutoSize = true };
        mgrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        mgrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 60));
        mgrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        mgrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 40));
        mgrid.Controls.Add(new Label { Text = "Provider", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) }, 0, 0);
        _agentProvider = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList, Margin = new Padding(3, 5, 12, 5) };
        _agentProvider.Items.AddRange(["ollama", "inferhub", "openai_compatible"]);
        _agentProvider.SelectedIndexChanged += async (_, _) =>
        {
            if (_agentProvider.Text == "ollama") await RefreshOllamaModelsAsync(showErrors: false);
        };
        mgrid.Controls.Add(_agentProvider, 1, 0);
        mgrid.Controls.Add(new Label { Text = "Num ctx", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) }, 2, 0);
        _agentNumCtxField = new TextBox { Dock = DockStyle.Fill, Margin = new Padding(3, 5, 3, 5), PlaceholderText = "blank = inherit" };
        mgrid.Controls.Add(_agentNumCtxField, 3, 0);
        mgrid.Controls.Add(new Label { Text = "Model", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) }, 0, 1);
        _agentModel = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDown, Margin = new Padding(3, 5, 12, 5) };
        mgrid.Controls.Add(_agentModel, 1, 1);
        mgrid.SetColumnSpan(_agentModel, 2);
        var applyModel = MakeButton("Apply", 90);
        applyModel.Click += async (_, _) => await ApplySelectedAgentModelAsync();
        mgrid.Controls.Add(applyModel, 3, 1);
        manage.Controls.Add(mgrid);

        var telegram = new GroupBox { Text = "Telegram", Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(12) };
        var tgrid = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 3, AutoSize = true };
        tgrid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        tgrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        tgrid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        _agentTelegramToken = new TextBox
        {
            Dock = DockStyle.Fill,
            Margin = new Padding(3, 5, 8, 5),
            PlaceholderText = "Paste a BotFather token to give this agent its own private bot...",
            UseSystemPasswordChar = true,
        };
        tgrid.Controls.Add(_agentTelegramToken, 0, 0);
        var setToken = MakeButton("Set", 80);
        setToken.Click += async (_, _) => await SetSelectedAgentTelegramAsync();
        tgrid.Controls.Add(setToken, 1, 0);
        var unsetToken = MakeButton("Unset", 80);
        unsetToken.Click += async (_, _) => await UnsetSelectedAgentTelegramAsync();
        tgrid.Controls.Add(unsetToken, 2, 0);
        _agentTelegramStatus = new Label
        {
            AutoSize = true,
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 8, 3, 0),
            Text = "No private bot -- reachable only through its own chat panel or the mesh-wide bot.",
        };
        tgrid.Controls.Add(_agentTelegramStatus, 0, 1);
        tgrid.SetColumnSpan(_agentTelegramStatus, 2);
        var testToken = MakeButton("Test", 80);
        testToken.Click += async (_, _) =>
        {
            if (_selectedAgent is { } row) await SendCommandAsync($"/telegram test {Quote(row.Name)}");
        };
        tgrid.Controls.Add(testToken, 2, 1);
        telegram.Controls.Add(tgrid);

        var chat = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        chat.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        chat.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        _agentChatOutput = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BackColor = Color.FromArgb(17, 25, 39),
            ForeColor = Color.FromArgb(225, 235, 245),
            Font = new Font("Cascadia Mono", 10F),
            BorderStyle = BorderStyle.FixedSingle,
            AllowDrop = true,
        };
        // A dropped file goes straight to the selected agent, same as the
        // Attach button -- the whole output pane is the drop target since
        // that is the larger, easier-to-hit surface.
        _agentChatOutput.DragEnter += (_, args) =>
        {
            args.Effect = args.Data?.GetDataPresent(DataFormats.FileDrop) == true
                ? DragDropEffects.Copy
                : DragDropEffects.None;
        };
        _agentChatOutput.DragDrop += async (_, args) =>
        {
            if (args.Data?.GetData(DataFormats.FileDrop) is string[] { Length: > 0 } paths)
            {
                await SendSelectedAgentFileAsync(paths[0]);
            }
        };
        // A FILE: line an agent's reply carries is rendered as a link (see
        // AppendChatLine); clicking anywhere inside that range opens it with
        // whatever the OS has associated with it -- the mesh and this app
        // run on the same machine, so the path is always locally readable.
        _agentChatOutput.MouseClick += (_, args) =>
        {
            var index = _agentChatOutput.GetCharIndexFromPosition(args.Location);
            var hit = _agentChatLinks.FirstOrDefault(
                link => index >= link.Start && index < link.Start + link.Length);
            if (hit.Path is not (null or ""))
            {
                OpenLocalFile(hit.Path);
            }
        };
        var chatInput = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, Padding = new Padding(0, 8, 0, 0) };
        chatInput.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        chatInput.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 90));
        chatInput.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 110));
        _agentChatInput = new TextBox { Dock = DockStyle.Fill, PlaceholderText = "Message this agent... (or drop a file above)" };
        _agentChatInput.KeyDown += async (_, args) =>
        {
            if (args.KeyCode == Keys.Enter && !args.Shift)
            {
                args.SuppressKeyPress = true;
                var text = _agentChatInput.Text.Trim();
                _agentChatInput.Clear();
                await SendSelectedAgentChatAsync(text);
            }
        };
        var attachButton = MakeButton("Attach", 80);
        attachButton.Dock = DockStyle.Fill;
        attachButton.Click += async (_, _) =>
        {
            using var dialog = new OpenFileDialog { Title = "Attach a file to send" };
            if (dialog.ShowDialog(this) == DialogResult.OK)
            {
                await SendSelectedAgentFileAsync(dialog.FileName);
            }
        };
        var sendButton = MakeButton("Send", 100);
        sendButton.Dock = DockStyle.Fill;
        sendButton.Click += async (_, _) =>
        {
            var text = _agentChatInput.Text.Trim();
            _agentChatInput.Clear();
            await SendSelectedAgentChatAsync(text);
        };
        chatInput.Controls.Add(_agentChatInput, 0, 0);
        chatInput.Controls.Add(attachButton, 1, 0);
        chatInput.Controls.Add(sendButton, 2, 0);
        chat.Controls.Add(_agentChatOutput, 0, 0);
        chat.Controls.Add(chatInput, 0, 1);

        layout.Controls.Add(header, 0, 0);
        layout.Controls.Add(actions, 0, 1);
        layout.Controls.Add(manage, 0, 2);
        layout.Controls.Add(telegram, 0, 3);
        layout.Controls.Add(chat, 0, 4);
        _agentDetail.Controls.Add(layout);

        container.Controls.Add(_agentDetail);
        container.Controls.Add(_agentPlaceholder);
        return container;
    }

    private async Task RefreshAgentListAsync()
    {
        if (!_runtime.IsRunning)
        {
            return;
        }
        List<AgentRow> rows;
        try
        {
            rows = await _runtime.GetAgentsAsync();
        }
        catch
        {
            // Transient -- the next timer tick tries again rather than
            // popping an error over what is, from a human's chair, nothing
            // more than a periodic background refresh.
            return;
        }
        _agentRows = rows;
        if (IsDisposed || Disposing)
        {
            return;
        }
        if (InvokeRequired)
        {
            try { BeginInvoke(ApplyAgentRows); } catch (ObjectDisposedException) { } catch (InvalidOperationException) { }
            return;
        }
        ApplyAgentRows();
    }

    private void ApplyAgentRows()
    {
        var previouslySelectedId = _selectedAgent?.Id;
        _agentList.BeginUpdate();
        _agentList.Items.Clear();
        foreach (var row in _agentRows.OrderBy(r => r.IsSystem ? 0 : 1).ThenBy(r => r.Name, StringComparer.OrdinalIgnoreCase))
        {
            var item = new ListViewItem(row.Name) { Tag = row, ForeColor = StatusColor(row) };
            item.SubItems.Add(StatusText(row));
            item.SubItems.Add($"{row.Provider}:{row.Model}");
            _agentList.Items.Add(item);
            if (row.Id == previouslySelectedId)
            {
                item.Selected = true;
            }
        }
        _agentList.EndUpdate();
        // The row objects are new instances every refresh; re-point the
        // selection at this tick's copy so the detail panel's own numbers
        // (cycles, phase, goal) do not go stale between polls.
        if (previouslySelectedId is not null)
        {
            var updated = _agentRows.FirstOrDefault(r => r.Id == previouslySelectedId);
            if (updated is not null)
            {
                _selectedAgent = updated;
                RenderAgentDetailHeader();
            }
        }
    }

    private static string StatusText(AgentRow row) => row.Phase switch
    {
        "offline" => "○ offline",
        "awaiting-harness" => "◐ working",
        "thinking" => "◐ thinking",
        "acting" => "◐ acting",
        "starting" => "◐ starting",
        "error" => "✕ error",
        _ => row.Status == "active" ? "● idle" : "○ stopped",
    };

    private static Color StatusColor(AgentRow row) => row.Phase switch
    {
        "offline" => Color.Gray,
        "error" => Color.Firebrick,
        _ => row.Status == "active" ? Color.FromArgb(20, 130, 60) : Color.DimGray,
    };

    /// <summary>Picks a row already in the cached list, e.g. after asking
    /// Architect for a new agent -- before the next refresh even lands.</summary>
    private void SelectAgentRowById(string agentId)
    {
        var row = _agentRows.FirstOrDefault(r => r.Id == agentId);
        if (row is null)
        {
            return;
        }
        foreach (ListViewItem item in _agentList.Items)
        {
            item.Selected = item.Tag is AgentRow tagged && tagged.Id == agentId;
        }
        SelectAgent(row);
    }

    private void SelectAgent(AgentRow? row)
    {
        _selectedAgent = row;
        if (row is null)
        {
            _agentDetail.Visible = false;
            _agentPlaceholder.Visible = true;
            return;
        }
        _agentPlaceholder.Visible = false;
        _agentDetail.Visible = true;
        RenderAgentDetailHeader();
        _agentProvider.SelectedItem = row.Provider;
        _agentModel.Text = row.Model;
        _agentNumCtxField.Text = row.NumCtx?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "";
        _agentTelegramToken.Clear();
        _agentChatOutput.Clear();
        _agentChatLinks.Clear();
        if (_agentChatHistory.TryGetValue(row.Id, out var history))
        {
            foreach (var line in history)
            {
                AppendChatLine(line);
            }
            _agentChatOutput.SelectionStart = _agentChatOutput.TextLength;
            _agentChatOutput.ScrollToCaret();
        }
    }

    private static readonly Regex FileReference = new(@"FILE: (.+)$", RegexOptions.Multiline);

    /// <summary>Appends one chat block, rendering any FILE: &lt;path&gt; line
    /// it carries as a clickable link instead of plain text.
    ///
    /// The backend already resolved the path to an absolute one (see
    /// ConsoleChannel._resolve_file_references) before this text ever
    /// arrived, so there is no path-resolution rule to duplicate here --
    /// just find the marker and color the part after it.</summary>
    private void AppendChatLine(string line)
    {
        var blockStart = _agentChatOutput.TextLength;
        _agentChatOutput.AppendText(line + Environment.NewLine);
        foreach (Match match in FileReference.Matches(line))
        {
            var group = match.Groups[1];
            var start = blockStart + group.Index;
            _agentChatLinks.Add((start, group.Length, group.Value.Trim()));
            _agentChatOutput.Select(start, group.Length);
            _agentChatOutput.SelectionColor = Color.FromArgb(120, 190, 255);
            _agentChatOutput.SelectionFont = new Font(_agentChatOutput.Font, FontStyle.Underline);
        }
        // Selection left at the file range above would bleed its color into
        // whatever AppendText adds next -- put it back at the end, in the
        // control's own default colors, before returning.
        _agentChatOutput.Select(_agentChatOutput.TextLength, 0);
        _agentChatOutput.SelectionColor = _agentChatOutput.ForeColor;
        _agentChatOutput.SelectionFont = _agentChatOutput.Font;
    }

    private void OpenLocalFile(string path)
    {
        try
        {
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
        catch (Exception exc) when (exc is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
        {
            MessageBox.Show(this, $"Could not open {path}: {exc.Message}", "Open file",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    private void RenderAgentDetailHeader()
    {
        if (_selectedAgent is not { } row)
        {
            return;
        }
        _agentDetailName.Text = row.Name + (row.IsSystem ? "  ·  core agent" : "");
        _agentDetailStatus.Text = $"{StatusText(row)}   cycles={row.Cycles}" + (row.Muted ? "   muted" : "")
            + (row.HasTelegram ? "   telegram" : "");
        _agentDetailStatus.ForeColor = StatusColor(row);
        _agentDetailMeta.Text = row switch
        {
            { Goal.Length: > 0 } => $"goal: {row.Goal}",
            { LastOutcome.Length: > 0 } => $"last: {row.LastOutcome}",
            _ => "idle, no open goal",
        };
        _agentStartStop.Text = row.Status == "active" ? "Stop" : "Start";
        _agentMuteToggle.Text = row.Muted ? "Unmute" : "Mute";
        // /agent delete refuses a core agent server-side too; graying the
        // button out here is one less round trip to learn that.
        _agentDeleteButton.Enabled = !row.IsSystem;
        _agentTelegramStatus.Text = row.HasTelegram
            ? "Has its own private bot -- \"Test\" asks it directly, or \"Unset\" to remove it."
            : "No private bot -- reachable only through its own chat panel or the mesh-wide bot.";
    }

    private async Task SendSelectedAgentChatAsync(string text)
    {
        if (_selectedAgent is not { } row || text.Length == 0)
        {
            return;
        }
        AppendAgentChat(row.Id, $"you> {text}");
        if (!_runtime.IsRunning)
        {
            AppendAgentChat(row.Id, "[start the mesh first]");
            return;
        }
        try
        {
            // Pins the shared control connection's chat target at this agent
            // right before asking -- the same /chat the Console tab uses, so
            // whichever surface talks next always says explicitly who to.
            await _runtime.RequestSilentAsync($"/chat {Quote(row.Name)}");
            var response = await _runtime.RequestSilentAsync(text);
            AppendAgentChat(row.Id, response);
        }
        catch (Exception exc)
        {
            AppendAgentChat(row.Id, $"[error] {exc.Message}");
        }
        await RefreshAgentListAsync();
    }

    private async Task SendSelectedAgentFileAsync(string localPath)
    {
        if (_selectedAgent is not { } row)
        {
            return;
        }
        AppendAgentChat(row.Id, $"you> [attaching {Path.GetFileName(localPath)}]");
        if (!_runtime.IsRunning)
        {
            AppendAgentChat(row.Id, "[start the mesh first]");
            return;
        }
        try
        {
            // Same pin-then-request shape as SendSelectedAgentChatAsync --
            // Desktop and the mesh process are on the same machine, so the
            // absolute local path from the dialog or drop is directly
            // readable by the Python side with no byte transfer needed.
            await _runtime.RequestSilentAsync($"/chat {Quote(row.Name)}");
            var response = await _runtime.RequestSilentAsync($"/attach {Quote(localPath)}");
            AppendAgentChat(row.Id, response);
        }
        catch (Exception exc)
        {
            AppendAgentChat(row.Id, $"[error] {exc.Message}");
        }
        await RefreshAgentListAsync();
    }

    private void AppendAgentChat(string agentId, string line)
    {
        if (!_agentChatHistory.TryGetValue(agentId, out var history))
        {
            history = [];
            _agentChatHistory[agentId] = history;
        }
        history.Add(line);
        if (_selectedAgent?.Id != agentId)
        {
            return;
        }
        AppendChatLine(line);
        _agentChatOutput.SelectionStart = _agentChatOutput.TextLength;
        _agentChatOutput.ScrollToCaret();
    }

    private async Task ToggleSelectedAgentRunningAsync()
    {
        if (_selectedAgent is not { } row) return;
        var action = row.Status == "active" ? "stop" : "start";
        await SendCommandAsync($"/agent {action} {Quote(row.Name)}");
        await RefreshAgentListAsync();
    }

    private async Task ToggleSelectedAgentMutedAsync()
    {
        if (_selectedAgent is not { } row) return;
        var action = row.Muted ? "unmute" : "mute";
        await SendCommandAsync($"/agent {action} {Quote(row.Name)}");
        await RefreshAgentListAsync();
    }

    private async Task ApplySelectedAgentModelAsync()
    {
        if (_selectedAgent is not { } row) return;
        if (string.IsNullOrWhiteSpace(_agentModel.Text))
        {
            MessageBox.Show(this, "Pick a model first.", "EvoMesh", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        await SendCommandAsync($"/model {Quote(row.Name)} {Quote(_agentModel.Text)} {_agentProvider.Text}");
        var numCtxText = _agentNumCtxField.Text.Trim();
        await SendCommandAsync(
            numCtxText.Length == 0
                ? $"/num-ctx {Quote(row.Name)} clear"
                : $"/num-ctx {Quote(row.Name)} {numCtxText}");
        await RefreshAgentListAsync();
    }

    private async Task SetSelectedAgentTelegramAsync()
    {
        if (_selectedAgent is not { } row) return;
        var token = _agentTelegramToken.Text.Trim();
        if (token.Length == 0)
        {
            MessageBox.Show(this, "Paste the token BotFather gave you first.", "Telegram", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        await SendCommandAsync($"/telegram set {Quote(row.Name)} {Quote(token)}");
        _agentTelegramToken.Clear();
        await RefreshAgentListAsync();
    }

    private async Task UnsetSelectedAgentTelegramAsync()
    {
        if (_selectedAgent is not { } row) return;
        await SendCommandAsync($"/telegram unset {Quote(row.Name)}");
        await RefreshAgentListAsync();
    }

    private async Task DeleteSelectedAgentAsync()
    {
        if (_selectedAgent is not { } row) return;
        if (row.IsSystem)
        {
            MessageBox.Show(
                this, $"'{row.Name}' is a core agent and cannot be deleted -- stop it instead.",
                "EvoMesh", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        var confirm = MessageBox.Show(
            this, $"Delete '{row.Name}' for good? This cannot be undone.",
            "Delete agent", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
        if (confirm != DialogResult.Yes) return;
        var wipe = MessageBox.Show(
            this, "Also delete its saved memory, context, and playground files from disk?",
            "Delete agent", MessageBoxButtons.YesNo, MessageBoxIcon.Question) == DialogResult.Yes;
        await SendCommandAsync($"/agent delete {Quote(row.Name)}{(wipe ? " wipe" : "")}");
        _agentChatHistory.Remove(row.Id);
        SelectAgent(null);
        await RefreshAgentListAsync();
    }

    private TabPage BuildSettingsTab()
    {
        var page = new TabPage("Settings") { Padding = new Padding(18), BackColor = BackColor, AutoScroll = true };
        var grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 4, Padding = new Padding(8) };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));

        _settingsNotice = new Label { AutoSize = true, Font = new Font(Font, FontStyle.Bold), ForeColor = Color.FromArgb(172, 83, 0), Margin = new Padding(3, 3, 3, 16) };
        grid.Controls.Add(_settingsNotice, 0, 0);
        grid.SetColumnSpan(_settingsNotice, 4);
        _environmentName = AddField(grid, "Environment name", 0, 1);
        _dataPath = AddField(grid, "SQLite data path", 2, 1);
        _generationPath = AddField(grid, "Generations path", 0, 2);
        _logLevel = AddCombo(grid, "Log level", 2, 2, ["DEBUG", "INFO", "WARNING", "ERROR"]);
        _defaultProvider = AddCombo(grid, "Default provider", 0, 3, ["ollama", "inferhub", "openai_compatible"]);
        var editSecrets = MakeButton("Edit secrets (API keys)...", 200);
        editSecrets.Click += (_, _) => OpenSecretsEditor();
        grid.Controls.Add(editSecrets, 2, 3);

        var row = 4;
        AddHelp(
            grid,
            "API keys typed here go straight into evomesh.yaml. Use \"...or key ref\" below " +
            "instead to keep the real key only in evomesh.secrets.yaml, which is never committed.",
            ref row);
        foreach (var name in new[] { "ollama", "inferhub", "openai_compatible" })
        {
            var heading = new Label { Text = name.Replace('_', ' ').ToUpperInvariant(), AutoSize = true, Font = new Font(Font, FontStyle.Bold), Margin = new Padding(3, 18, 3, 6) };
            grid.Controls.Add(heading, 0, row);
            grid.SetColumnSpan(heading, 4);
            row++;
            var url = AddField(grid, "Base URL", 0, row);
            var model = name == "ollama"
                ? AddEditableCombo(
                    grid,
                    "Default model",
                    2,
                    row,
                    () => RefreshOllamaModelsAsync(showErrors: true))
                : AddEditableCombo(grid, "Default model", 2, row);
            row++;
            var key = AddField(grid, "API key (optional)", 0, row);
            key.UseSystemPasswordChar = true;
            var keyRef = AddEditableCombo(grid, "...or key ref (from secrets file)", 2, row);
            row++;
            var numCtx = AddField(grid, "Context window (num_ctx)", 0, row);
            grid.Controls.Add(
                new Label
                {
                    Text = "Tokens Ollama allocates per request; ignored by other providers.",
                    AutoSize = true,
                    ForeColor = Color.DimGray,
                    Margin = new Padding(3, 8, 8, 8),
                },
                2,
                row);
            _providers[name] = (url, model, key, numCtx, keyRef);
            row++;
        }

        AddHeading(grid, "RUNTIME", ref row);
        AddHelp(
            grid,
            "How often an agent's BDI cycle fires, and how often the Evolver advances one pipeline " +
            "stage. Longer intervals mean less GPU load per hour -- a slower model, or a machine " +
            "you need for other work at the same time, both call for raising these -- at the cost " +
            "of the mesh reacting more slowly.",
            ref row);
        _runtimeCycleSeconds = AddField(grid, "Agent cycle seconds", 0, row);
        _evolutionCycleSeconds = AddField(grid, "Evolver cycle seconds", 2, row);
        row++;

        AddHeading(grid, "EVOLUTION & GIT", ref row);
        AddHelp(
            grid,
            "With promotion on, a generation the mesh validates is committed on its own verdict, " +
            "pushed to the remote, and the mesh restarts itself into it. Off, it waits in the " +
            "console for /evolution promote or discard. Commits are authored by the identity " +
            "below, so the agent's work is never mistaken for yours.",
            ref row);
        _autoPromote = AddCheck(grid, "Promote a validated generation automatically", 0, row);
        row++;
        _autoRestart = AddCheck(grid, "Restart into a landed generation", 0, row);
        _gitAutoPush = AddCheck(grid, "Push a landed generation to the remote", 2, row);
        row++;
        _gitAuthorName = AddField(grid, "Commit author name", 0, row);
        _gitAuthorEmail = AddField(grid, "Commit author email", 2, row);
        row++;
        _gitRemote = AddField(grid, "Remote", 0, row);
        _gitBranch = AddField(grid, "Branch (blank = current)", 2, row);
        row++;

        AddHeading(grid, "TELEGRAM", ref row);
        AddHelp(
            grid,
            "Create a bot with @BotFather, paste the token it gives you, and enable it. Everything " +
            "you send the bot goes through the same commands as this console. Leave the chat ids " +
            "empty with adoption on and the first person to send /start claims the bot.",
            ref row);
        _telegramEnabled = AddCheck(grid, "Enable the Telegram bot", 0, row);
        _telegramAdoptFirst = AddCheck(grid, "Let the first chat claim the bot", 2, row);
        row++;
        _telegramToken = AddField(grid, "Bot token (BotFather)", 0, row);
        _telegramToken.UseSystemPasswordChar = true;
        grid.SetColumnSpan(_telegramToken, 3);
        row++;
        _telegramChats = AddField(grid, "Allowed chat ids", 0, row);
        _telegramAnnouncements = AddCheck(grid, "Announce promotions and restarts", 2, row);
        row++;

        var telegramActions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill };
        var testToken = MakeButton("Test token", 130);
        testToken.Click += async (_, _) => await TestTelegramTokenAsync();
        var liveStatus = MakeButton("Live status", 130);
        liveStatus.Click += async (_, _) => await SendCommandAsync("/telegram status");
        var allowChat = MakeButton("Allow chat id", 145);
        allowChat.Click += async (_, _) => await ManageTelegramChatAsync("allow");
        var revokeChat = MakeButton("Revoke chat id", 150);
        revokeChat.Click += async (_, _) => await ManageTelegramChatAsync("revoke");
        telegramActions.Controls.AddRange([testToken, liveStatus, allowChat, revokeChat]);
        grid.Controls.Add(telegramActions, 0, row);
        grid.SetColumnSpan(telegramActions, 4);
        row++;
        _telegramNotice = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(880, 0),
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
            Text = "\"Test token\" asks Telegram directly and needs neither a save nor a "
                 + "running mesh. \"Live status\" asks the running mesh, and is the only "
                 + "place a chat that claimed the bot at runtime shows up — those are kept "
                 + "in the database, not in this file.",
        };
        grid.Controls.Add(_telegramNotice, 0, row);
        grid.SetColumnSpan(_telegramNotice, 4);
        row++;

        var systemHeading = new Label
        {
            Text = "CORE AGENT MODELS",
            AutoSize = true,
            Font = new Font(Font, FontStyle.Bold),
            Margin = new Padding(3, 18, 3, 6),
        };
        grid.Controls.Add(systemHeading, 0, row);
        grid.SetColumnSpan(systemHeading, 4);
        row++;
        var systemHelp = new Label
        {
            Text = "These provider/model assignments are applied to the built-in agents on the next mesh start.",
            AutoSize = true,
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
        };
        grid.Controls.Add(systemHelp, 0, row);
        grid.SetColumnSpan(systemHelp, 4);
        row++;
        foreach (var (agentId, agentName) in CoreAgents)
        {
            var provider = AddCombo(
                grid,
                agentName,
                0,
                row,
                ["ollama", "inferhub", "openai_compatible"]);
            var model = AddEditableCombo(grid, "Model", 2, row);
            provider.SelectedIndexChanged += async (_, _) =>
            {
                if (!_loadingSettings && provider.Text == "ollama")
                {
                    await RefreshOllamaModelsAsync(showErrors: false);
                }
            };
            row++;
            var numCtx = AddField(grid, "Context window override (blank = inherit)", 0, row);
            _systemAgents[agentId] = (provider, model, numCtx);
            row++;
        }

        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(3, 18, 3, 3) };
        _saveSettings = MakeButton("Save settings", 145);
        _saveSettings.Click += async (_, _) => await SaveSettingsAsync();
        _reloadSettings = MakeButton("Reload", 110);
        _reloadSettings.Click += (_, _) => LoadSettings();
        actions.Controls.AddRange([_saveSettings, _reloadSettings]);
        grid.Controls.Add(actions, 0, row);
        grid.SetColumnSpan(actions, 4);

        page.Controls.Add(grid);
        return page;
    }

    /// <summary>
    /// Asks Telegram itself whether the token in the box is real.
    /// </summary>
    /// <remarks>
    /// Deliberately independent of the mesh and of saving. A human who has just
    /// pasted a token wants to know it is right before committing to a restart,
    /// and the failure they need to see -- a typo, a revoked bot, no network --
    /// is one only Telegram can report.
    /// </remarks>
    private async Task TestTelegramTokenAsync()
    {
        var token = _telegramToken.Text.Trim();
        if (token.Length == 0)
        {
            MessageBox.Show(this, "Paste the token BotFather gave you first.", "Telegram",
                MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            using var response = await client.GetAsync($"https://api.telegram.org/bot{token}/getMe");
            using var document = JsonDocument.Parse(await response.Content.ReadAsStreamAsync());
            var root = document.RootElement;
            if (!response.IsSuccessStatusCode || !root.GetProperty("ok").GetBoolean())
            {
                var reason = root.TryGetProperty("description", out var description)
                    ? description.GetString()
                    : $"HTTP {(int)response.StatusCode}";
                AppendOutput($"[Telegram refused the token] {reason}");
                MessageBox.Show(this, $"Telegram refused the token:{Environment.NewLine}{Environment.NewLine}{reason}", "Telegram",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            var bot = root.GetProperty("result");
            var name = bot.TryGetProperty("username", out var username) ? username.GetString() : "?";
            AppendOutput($"[Telegram accepted the token] the bot is @{name}");
            var newline = Environment.NewLine;
            MessageBox.Show(this,
                $"Telegram accepted the token.{newline}{newline}The bot is @{name}.{newline}{newline}" +
                "Save the settings and restart the mesh, then send it /start from Telegram.",
                "Telegram", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception exc)
        {
            AppendOutput($"[Telegram could not be reached] {exc.Message}");
            MessageBox.Show(this, exc.Message, "Telegram is unreachable",
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    /// <summary>Adds or removes one chat id on the running mesh.</summary>
    private async Task ManageTelegramChatAsync(string action)
    {
        var id = _telegramChats.Text.Split(',').LastOrDefault()?.Trim() ?? "";
        if (!long.TryParse(id, out _))
        {
            MessageBox.Show(this,
                $"Put the chat id to {action} last in the \"Allowed chat ids\" box, " +
                "then press this again.",
                "Telegram", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        await SendCommandAsync($"/telegram {action} {id}");
    }

    private async Task SendCurrentAsync()
    {
        var text = _command.Text.Trim();
        if (text.Length == 0) return;
        _command.Clear();
        await SendCommandAsync(text);
    }

    private async Task RefreshOllamaModelsAsync(bool showErrors)
    {
        if (_agentProvider is null || _agentModel is null)
        {
            return;
        }
        try
        {
            var settings = EvoMeshYamlSettings.Load(_configPath);
            if (!settings.Providers.TryGetValue("ollama", out var ollama))
            {
                throw new InvalidOperationException("Ollama is not configured in evomesh.yaml.");
            }
            var baseUrl = ollama.BaseUrl.TrimEnd('/');
            var tagsUrl = baseUrl.EndsWith("/api", StringComparison.OrdinalIgnoreCase)
                ? $"{baseUrl}/tags"
                : $"{baseUrl}/api/tags";
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var response = await client.GetAsync(tagsUrl);
            response.EnsureSuccessStatusCode();
            using var document = JsonDocument.Parse(await response.Content.ReadAsStreamAsync());
            var names = document.RootElement.GetProperty("models")
                .EnumerateArray()
                .Select(item => item.GetProperty("name").GetString())
                .Where(name => !string.IsNullOrWhiteSpace(name))
                .Cast<string>()
                .Order(StringComparer.OrdinalIgnoreCase)
                .ToArray();
            if (_agentProvider.Text == "ollama")
            {
                PopulateModelCombo(_agentModel, names, ollama.Model);
            }
            if (_providers.TryGetValue("ollama", out var settingsControls))
            {
                PopulateModelCombo(settingsControls.Model, names, ollama.Model);
            }
            foreach (var (agentId, controls) in _systemAgents)
            {
                if (controls.Provider.Text != "ollama")
                {
                    continue;
                }
                var configuredModel = settings.SystemAgents.TryGetValue(agentId, out var agent)
                    ? agent.Model
                    : ollama.Model;
                PopulateModelCombo(controls.Model, names, configuredModel);
            }
            AppendOutput($"[loaded {names.Length} Ollama models into the dropdowns]");
        }
        catch (Exception exc)
        {
            AppendOutput($"[Ollama models unavailable] {exc.Message}");
            if (showErrors)
            {
                MessageBox.Show(this, exc.Message, "Unable to load Ollama models", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }

    private static void PopulateModelCombo(ComboBox combo, string[] models, string configuredModel)
    {
        var current = combo.Text;
        combo.BeginUpdate();
        combo.Items.Clear();
        combo.Items.AddRange(models);
        combo.EndUpdate();

        var preferred = string.IsNullOrWhiteSpace(current) ? configuredModel : current;
        var match = models.FirstOrDefault(name =>
            string.Equals(name, preferred, StringComparison.OrdinalIgnoreCase));
        if (match is not null)
        {
            combo.SelectedItem = match;
        }
        else if (!string.IsNullOrWhiteSpace(preferred))
        {
            combo.Text = preferred;
        }
        else if (models.Length > 0)
        {
            combo.SelectedIndex = 0;
        }
    }

    private async Task SendCommandAsync(string command)
    {
        if (string.IsNullOrWhiteSpace(command)) return;
        if (!_runtime.IsRunning)
        {
            MessageBox.Show(this, "Start EvoMesh first.", "EvoMesh", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }
        AppendOutput($"you> {command}");
        await RunSafeAsync(() => _runtime.SendAsync(command));
    }

    private void LoadSettings()
    {
        EnsureConfiguration();
        var settings = EvoMeshYamlSettings.Load(_configPath);
        _loadingSettings = true;
        try
        {
            _environmentName.Text = settings.EnvironmentName;
            _dataPath.Text = settings.DataPath;
            _generationPath.Text = settings.GenerationPath;
            _logLevel.SelectedItem = settings.LogLevel.ToUpperInvariant();
            _defaultProvider.SelectedItem = settings.DefaultProvider;
            _secrets = EvoMeshSecretsSettings.Load(_secretsPath);
            var refs = _secrets.Refs.Keys.ToArray();
            foreach (var (name, controls) in _providers)
            {
                controls.KeyRef.Items.Clear();
                controls.KeyRef.Items.AddRange(refs);
                if (settings.Providers.TryGetValue(name, out var provider))
                {
                    controls.Url.Text = provider.BaseUrl;
                    controls.Model.Text = provider.Model;
                    controls.Key.Text = provider.ApiKey;
                    controls.KeyRef.Text = provider.ApiKeyRef;
                    controls.NumCtx.Text = provider.NumCtx.ToString(System.Globalization.CultureInfo.InvariantCulture);
                }
            }
            _runtimeCycleSeconds.Text = settings.Runtime.CycleSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
            _evolutionCycleSeconds.Text = settings.Evolution.CycleSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
            _autoPromote.Checked = settings.Evolution.AutoPromote;
            _autoRestart.Checked = settings.Evolution.AutoRestart;
            _gitAuthorName.Text = settings.Git.AuthorName;
            _gitAuthorEmail.Text = settings.Git.AuthorEmail;
            _gitAutoPush.Checked = settings.Git.AutoPush;
            _gitRemote.Text = settings.Git.Remote;
            _gitBranch.Text = settings.Git.Branch;
            _telegramEnabled.Checked = settings.Telegram.Enabled;
            _telegramToken.Text = settings.Telegram.Token;
            _telegramChats.Text = settings.Telegram.AllowedChatIds;
            _telegramAdoptFirst.Checked = settings.Telegram.AdoptFirstChat;
            _telegramAnnouncements.Checked = settings.Telegram.Announcements;
            foreach (var (agentId, controls) in _systemAgents)
            {
                var configured = settings.SystemAgents.GetValueOrDefault(agentId);
                var providerName = configured?.Provider ?? settings.DefaultProvider;
                var modelName = configured?.Model;
                if (string.IsNullOrWhiteSpace(modelName) &&
                    settings.Providers.TryGetValue(providerName, out var provider))
                {
                    modelName = provider.Model;
                }
                controls.Provider.SelectedItem = providerName;
                controls.Model.Text = modelName ?? "local-model";
                controls.NumCtx.Text = configured?.NumCtx?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "";
            }
        }
        finally
        {
            _loadingSettings = false;
        }
    }

    private async Task SaveSettingsAsync()
    {
        if (_runtime.IsRunning)
        {
            // Every setting on this tab is read once, at boot. Offering the
            // restart here is the difference between a saved file and a mesh
            // that is actually using it.
            var choice = MessageBox.Show(
                this,
                "These settings are read when the mesh boots. Save them and restart the mesh now?",
                "Restart required",
                MessageBoxButtons.YesNoCancel,
                MessageBoxIcon.Question);
            if (choice == DialogResult.Cancel)
            {
                return;
            }
            WriteSettings();
            if (choice == DialogResult.Yes)
            {
                await SendCommandAsync("/restart");
            }
            else
            {
                AppendOutput("[saved; the mesh keeps running on the settings it booted with]");
            }
            return;
        }
        WriteSettings();
    }

    /// <summary>
    /// Writes the editor's values over the file that is already there.
    /// </summary>
    /// <remarks>
    /// Loading first is what keeps this honest: the tab does not show every
    /// setting the mesh has, and building a fresh object would quietly reset
    /// each one it does not show -- the evolution objective, the prompt
    /// budgets -- to a default nobody asked for.
    /// </remarks>
    private void WriteSettings()
    {
        var settings = EvoMeshYamlSettings.Load(_configPath);
        settings.EnvironmentName = _environmentName.Text.Trim();
        settings.DataPath = _dataPath.Text.Trim();
        settings.GenerationPath = _generationPath.Text.Trim();
        settings.LogLevel = _logLevel.Text;
        settings.DefaultProvider = _defaultProvider.Text;
        settings.Runtime.CycleSeconds = int.TryParse(_runtimeCycleSeconds.Text.Trim(), out var runtimeCycle) && runtimeCycle > 0
            ? runtimeCycle
            : settings.Runtime.CycleSeconds;
        settings.Evolution.CycleSeconds = int.TryParse(_evolutionCycleSeconds.Text.Trim(), out var evolutionCycle) && evolutionCycle > 0
            ? evolutionCycle
            : settings.Evolution.CycleSeconds;
        settings.Evolution.AutoPromote = _autoPromote.Checked;
        settings.Evolution.AutoRestart = _autoRestart.Checked;
        settings.Git.AuthorName = _gitAuthorName.Text.Trim();
        settings.Git.AuthorEmail = _gitAuthorEmail.Text.Trim();
        settings.Git.AutoPush = _gitAutoPush.Checked;
        settings.Git.Remote = _gitRemote.Text.Trim();
        settings.Git.Branch = _gitBranch.Text.Trim();
        settings.Telegram.Enabled = _telegramEnabled.Checked;
        settings.Telegram.Token = _telegramToken.Text.Trim();
        settings.Telegram.AllowedChatIds = _telegramChats.Text.Trim();
        settings.Telegram.AdoptFirstChat = _telegramAdoptFirst.Checked;
        settings.Telegram.Announcements = _telegramAnnouncements.Checked;
        foreach (var (name, controls) in _providers)
        {
            var existing = settings.Providers.GetValueOrDefault(name);
            var keyRef = controls.KeyRef.Text.Trim();
            var updated = new ProviderEditorSettings(
                controls.Url.Text.Trim(),
                controls.Model.Text.Trim(),
                // A ref wins: the literal box is only what config.py's loader
                // actually reads when no ref is set (see ProviderEditorSettings'
                // own remark, and EvoMeshYamlSettings.Save's matching choice).
                keyRef.Length == 0 ? controls.Key.Text : "",
                existing?.TimeoutSeconds ?? 600,
                int.TryParse(controls.NumCtx.Text.Trim(), out var numCtx) && numCtx > 0
                    ? numCtx
                    : existing?.NumCtx ?? 65536,
                keyRef);
            // The record's constructor gives ModelNumCtx a fresh, empty dictionary;
            // a per-model entry a human hand-edited into the file has no editor
            // control at all, so it is copied across rather than dropped here.
            if (existing is not null)
            {
                foreach (var (tag, tagNumCtx) in existing.ModelNumCtx)
                {
                    updated.ModelNumCtx[tag] = tagNumCtx;
                }
            }
            settings.Providers[name] = updated;
        }
        foreach (var (agentId, controls) in _systemAgents)
        {
            var numCtxText = controls.NumCtx.Text.Trim();
            settings.SystemAgents[agentId] = new AgentModelEditorSettings(
                controls.Provider.Text,
                controls.Model.Text.Trim(),
                int.TryParse(numCtxText, out var agentNumCtx) && agentNumCtx > 0 ? agentNumCtx : null);
        }
        settings.Save(_configPath);
        AppendOutput($"[settings saved to {_configPath}]");
    }

    /// <summary>
    /// A small modal grid over evomesh.secrets.yaml's ref -&gt; key pairs --
    /// the only screen in this app that ever shows or writes a real key.
    /// </summary>
    /// <remarks>
    /// Reloads from disk on open (not from the in-memory _secrets, which may
    /// be stale from whenever the Settings tab last loaded) and, on Save,
    /// refreshes every provider row's "key ref" combo so a ref added here is
    /// immediately pickable without reopening the tab.
    /// </remarks>
    private void OpenSecretsEditor()
    {
        var secrets = EvoMeshSecretsSettings.Load(_secretsPath);

        using var dialog = new Form
        {
            Text = "Edit secrets (evomesh.secrets.yaml)",
            StartPosition = FormStartPosition.CenterParent,
            Size = new Size(560, 420),
            MinimumSize = new Size(420, 300),
            Font = Font,
            BackColor = BackColor,
        };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, Padding = new Padding(14) };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var help = new Label
        {
            AutoSize = true,
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 10),
            Text = "Real API keys, named by ref. Written only to evomesh.secrets.yaml, which " +
                   ".gitignore keeps out of every commit -- point a provider's \"key ref\" field " +
                   "at the name you give one here instead of pasting the key into evomesh.yaml.",
            MaximumSize = new Size(520, 0),
        };
        layout.Controls.Add(help, 0, 0);

        var grid = new DataGridView
        {
            Dock = DockStyle.Fill,
            AllowUserToAddRows = true,
            AllowUserToDeleteRows = true,
            AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill,
            RowHeadersVisible = false,
            BackgroundColor = Color.White,
        };
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Ref", HeaderText = "Ref" });
        grid.Columns.Add(new DataGridViewTextBoxColumn { Name = "Key", HeaderText = "API key" });
        foreach (var (refName, key) in secrets.Refs)
        {
            grid.Rows.Add(refName, key);
        }
        layout.Controls.Add(grid, 0, 1);

        var actions = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Right,
            FlowDirection = FlowDirection.RightToLeft,
            Margin = new Padding(3, 10, 3, 0),
        };
        var cancel = MakeButton("Cancel", 90);
        cancel.DialogResult = DialogResult.Cancel;
        var save = MakeButton("Save", 90);
        save.DialogResult = DialogResult.OK;
        actions.Controls.AddRange([cancel, save]);
        layout.Controls.Add(actions, 0, 2);

        dialog.Controls.Add(layout);
        dialog.AcceptButton = save;
        dialog.CancelButton = cancel;

        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            return;
        }

        secrets.Refs.Clear();
        foreach (DataGridViewRow gridRow in grid.Rows)
        {
            var refName = Convert.ToString(gridRow.Cells["Ref"].Value)?.Trim() ?? "";
            var key = Convert.ToString(gridRow.Cells["Key"].Value) ?? "";
            if (refName.Length > 0)
            {
                secrets.Refs[refName] = key;
            }
        }
        secrets.Save(_secretsPath);
        _secrets = secrets;
        var refs = secrets.Refs.Keys.ToArray();
        foreach (var (_, controls) in _providers)
        {
            var current = controls.KeyRef.Text;
            controls.KeyRef.Items.Clear();
            controls.KeyRef.Items.AddRange(refs);
            controls.KeyRef.Text = current;
        }
        AppendOutput($"[secrets saved to {_secretsPath}]");
    }

    private void EnsureConfiguration()
    {
        if (!File.Exists(_configPath))
        {
            File.Copy(Path.Combine(_runtime.RootPath, "evomesh.yaml.example"), _configPath);
        }
    }

    private void UpdateRuntimeState(bool running)
    {
        // Raised from a background health-loop/process-exit callback that is
        // not wrapped in a Task -- an exception here (e.g. BeginInvoke on a
        // window the user already closed) would kill the entire Control
        // Center process, not just this UI update, taking down the only
        // thing watching the mesh for its restart-on-new-generation exit
        // code. A closed window means there is nothing to update; that is
        // not a reason to stop supervising the mesh.
        if (IsDisposed || Disposing)
        {
            return;
        }
        if (InvokeRequired)
        {
            try
            {
                BeginInvoke(() => UpdateRuntimeState(running));
            }
            catch (ObjectDisposedException) { }
            catch (InvalidOperationException) { }
            return;
        }
        _running = running;
        RenderStatus();
        _start.Enabled = !running;
        _stop.Enabled = running;
        // The settings stay editable while the mesh runs: saving now offers the
        // restart that makes them take effect, which beats making a human stop
        // the mesh by hand just to type a token into a box.
        _settingsNotice.Text = running
            ? "Mesh is running. Saving asks whether to restart it so the new settings take effect."
            : "Mesh is stopped. Settings can be edited and will apply on the next start.";
    }

    /// <summary>
    /// Puts the time of the last check on screen next to the verdict. A status
    /// with no timestamp cannot be told apart from one nobody has re-examined
    /// since the window opened, which is exactly the confusion this fixes.
    /// </summary>
    private void ShowHealthCheck(bool running, DateTimeOffset when)
    {
        if (IsDisposed || Disposing)
        {
            return;
        }
        if (InvokeRequired)
        {
            try
            {
                BeginInvoke(() => ShowHealthCheck(running, when));
            }
            catch (ObjectDisposedException) { }
            catch (InvalidOperationException) { }
            return;
        }
        _running = running;
        _lastCheck = when;
        RenderStatus();
    }

    private void RenderStatus()
    {
        var checkedAt = _lastCheck is null ? "" : $"  checked {_lastCheck:HH:mm:ss}";
        _status.Text = (_running ? "● RUNNING" : "● STOPPED") + checkedAt;
        _status.ForeColor = _running ? Color.FromArgb(78, 220, 130) : Color.FromArgb(255, 170, 120);
    }

    private void AppendOutput(string text)
    {
        if (IsDisposed || Disposing)
        {
            return;
        }
        if (InvokeRequired)
        {
            try
            {
                BeginInvoke(() => AppendOutput(text));
            }
            catch (ObjectDisposedException) { }
            catch (InvalidOperationException) { }
            return;
        }
        _output.AppendText(text + Environment.NewLine);
        _output.SelectionStart = _output.TextLength;
        _output.ScrollToCaret();
    }

    private async Task RunSafeAsync(Func<Task> action)
    {
        try { await action(); }
        catch (Exception exc)
        {
            AppendOutput($"[error] {exc.Message}");
            MessageBox.Show(this, exc.Message, "EvoMesh error", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static Button MakeButton(string text, int width)
    {
        var button = new Button
        {
            Text = text,
            Width = width,
            Height = 36,
            Margin = new Padding(4),
            // Grows past `width` when the text needs it (in the font it ends
            // up with), instead of clipping it -- "Improvements" did not fit 120.
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowOnly,
            MinimumSize = new Size(width, 36),
        };
        StyleButton(button);
        return button;
    }

    private static void StyleButton(Button button)
    {
        button.FlatStyle = FlatStyle.Flat;
        button.UseVisualStyleBackColor = false;
        button.FlatAppearance.BorderColor = Color.FromArgb(30, 112, 168);
        button.FlatAppearance.BorderSize = 1;

        void ApplyColors()
        {
            button.BackColor = button.Enabled
                ? Color.FromArgb(14, 82, 132)
                : Color.FromArgb(215, 222, 229);
            button.ForeColor = button.Enabled
                ? Color.White
                : Color.FromArgb(80, 88, 96);
        }

        button.EnabledChanged += (_, _) => ApplyColors();
        ApplyColors();
    }

    private void AddHeading(TableLayoutPanel grid, string text, ref int row)
    {
        var heading = new Label
        {
            Text = text,
            AutoSize = true,
            Font = new Font(Font, FontStyle.Bold),
            Margin = new Padding(3, 18, 3, 6),
        };
        grid.Controls.Add(heading, 0, row);
        grid.SetColumnSpan(heading, 4);
        row++;
    }

    private static void AddHelp(TableLayoutPanel grid, string text, ref int row)
    {
        var help = new Label
        {
            Text = text,
            AutoSize = true,
            MaximumSize = new Size(880, 0),
            ForeColor = Color.DimGray,
            Margin = new Padding(3, 0, 3, 8),
        };
        grid.Controls.Add(help, 0, row);
        grid.SetColumnSpan(help, 4);
        row++;
    }

    private static CheckBox AddCheck(TableLayoutPanel grid, string label, int column, int row)
    {
        var field = new CheckBox
        {
            Text = label,
            AutoSize = true,
            Anchor = AnchorStyles.Left,
            Margin = new Padding(3, 8, 8, 8),
        };
        grid.Controls.Add(field, column, row);
        grid.SetColumnSpan(field, 2);
        return field;
    }

    private static TextBox AddField(TableLayoutPanel grid, string label, int column, int row)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new TextBox { Dock = DockStyle.Fill, Margin = new Padding(3, 5, 12, 5) };
        grid.Controls.Add(caption, column, row);
        grid.Controls.Add(field, column + 1, row);
        return field;
    }

    private static ComboBox AddCombo(TableLayoutPanel grid, string label, int column, int row, string[] items)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList, Margin = new Padding(3, 5, 12, 5) };
        field.Items.AddRange(items);
        grid.Controls.Add(caption, column, row);
        grid.Controls.Add(field, column + 1, row);
        return field;
    }

    private ComboBox AddEditableCombo(
        TableLayoutPanel grid,
        string label,
        int column,
        int row,
        Func<Task>? refresh = null)
    {
        var caption = new Label { Text = label, AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(3, 8, 8, 8) };
        var field = new ComboBox
        {
            Dock = DockStyle.Fill,
            DropDownStyle = ComboBoxStyle.DropDown,
            AutoCompleteMode = AutoCompleteMode.SuggestAppend,
            AutoCompleteSource = AutoCompleteSource.ListItems,
            Margin = refresh is null ? new Padding(3, 5, 12, 5) : Padding.Empty,
        };
        grid.Controls.Add(caption, column, row);
        if (refresh is null)
        {
            grid.Controls.Add(field, column + 1, row);
        }
        else
        {
            var container = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 2,
                RowCount = 1,
                Margin = new Padding(3, 5, 12, 5),
            };
            container.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            container.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 88));
            var refreshButton = MakeButton("Refresh", 80);
            refreshButton.Dock = DockStyle.Fill;
            refreshButton.Margin = new Padding(6, 0, 0, 0);
            refreshButton.Click += async (_, _) => await refresh();
            container.Controls.Add(field, 0, 0);
            container.Controls.Add(refreshButton, 1, 0);
            grid.Controls.Add(container, column + 1, row);
        }
        return field;
    }

    private static string Quote(string value) => $"\"{value.Replace("\\", "\\\\").Replace("\"", "\\\"")}\"";
}
