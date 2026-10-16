using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Sts2Headless;

class Program
{
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
        // WhenWritingNull only applies to object properties; this also drops null values of
        // nested dictionaries (e.g. a card's "keywords": null), which make up most of them.
        Converters = { new SkipNullValuesConverter() },
    };

    private sealed class SkipNullValuesConverter : JsonConverter<Dictionary<string, object?>>
    {
        public override Dictionary<string, object?>? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
            throw new NotSupportedException();

        public override void Write(Utf8JsonWriter writer, Dictionary<string, object?> value, JsonSerializerOptions options)
        {
            // A response's own top-level fields are its schema and always present; nested
            // objects (cards, relics, powers, ...) omit their null fields.
            var skipNulls = writer.CurrentDepth > 0;
            writer.WriteStartObject();
            foreach (var (k, v) in value)
            {
                if (v == null)
                {
                    if (skipNulls) continue;
                    writer.WriteNull(k);
                    continue;
                }
                writer.WritePropertyName(k);
                JsonSerializer.Serialize(writer, v, v.GetType(), options);
            }
            writer.WriteEndObject();
        }
    }

    /// <summary>
    /// Locate the directory containing sts2.dll: STS2_LIB env, walk up from BaseDirectory, then BaseDirectory/lib.
    /// </summary>
    private static string ResolveLibDirectory()
    {
        var envLib = Environment.GetEnvironmentVariable("STS2_LIB");
        if (!string.IsNullOrWhiteSpace(envLib))
        {
            var p = Path.GetFullPath(envLib.Trim());
            if (Directory.Exists(p) && File.Exists(Path.Combine(p, "sts2.dll")))
                return p;
        }

        var dir = AppContext.BaseDirectory;
        for (var depth = 0; depth < 16 && !string.IsNullOrEmpty(dir); depth++)
        {
            var candidate = Path.Combine(dir, "lib");
            if (Directory.Exists(candidate) && File.Exists(Path.Combine(candidate, "sts2.dll")))
                return Path.GetFullPath(candidate);
            dir = Directory.GetParent(dir)?.FullName ?? "";
        }

        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "lib"));
    }

    // stdout carries only protocol JSON. Console.Out is pointed at stderr so that output
    // from game/library code (e.g. Sentry's startup notice) can't interleave with it.
    // Responses are written as raw UTF-8, one write() per line (Console.Out would flush every
    // 256 chars and wake the reader on partial lines).
    private static Stream ProtocolOut = Stream.Null;
    private static readonly object ProtocolLock = new();
    private static System.Buffers.ArrayBufferWriter<byte> ProtocolBuf = new(1 << 16);
    private static Utf8JsonWriter? ProtocolJson;

    static void Main(string[] args)
    {
        // Assigned here, not only in the initializer: static field initializers may run
        // lazily (beforefieldinit), i.e. after SetOut below.
        ProtocolOut = Console.OpenStandardOutput();
        Console.SetOut(Console.Error);

        // An unhandled exception on a non-main thread terminates the process. Tell the
        // client why before that happens instead of leaving it with a bare EOF.
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
        {
            Console.Error.WriteLine($"[FATAL] Unhandled: {e.ExceptionObject}");
            try
            {
                var ex = e.ExceptionObject as Exception;
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "fatal",
                    ["message"] = ex != null ? $"{ex.GetType().Name}: {ex.Message}" : e.ExceptionObject?.ToString(),
                    ["stack_trace"] = e.ExceptionObject?.ToString(),
                });
            }
            catch { }
        };
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Console.Error.WriteLine($"[WARN] Unobserved task exception: {e.Exception}");
            e.SetObserved();
        };

        var libDir = ResolveLibDirectory();
        DataDirs.LibDir = libDir;

        AssemblyLoadContext.Default.Resolving += (ctx, name) =>
        {
            var path = Path.Combine(libDir, name.Name + ".dll");
            if (File.Exists(path))
                return ctx.LoadFromAssemblyPath(Path.GetFullPath(path));

            // Also check game directory (via STS2_GAME_DIR env var)
            var gameDir = Environment.GetEnvironmentVariable("STS2_GAME_DIR") ?? "";
            if (!string.IsNullOrEmpty(gameDir))
            {
                path = Path.Combine(gameDir, name.Name + ".dll");
                if (File.Exists(path))
                    return ctx.LoadFromAssemblyPath(path);
            }

            return null;
        };

        // All game logic runs on one dedicated engine thread, which also reads the commands and
        // writes the responses, so a normal command costs no cross-thread handoff. This thread
        // is the watchdog: if a command runs past its deadline (a game flow that never
        // completes), it answers engine_timeout itself and takes over stdin, refusing further
        // commands so the client restarts the process.
        if (args.Contains("--debug")) EnableDebugCommands();
        var timeout = TimeSpan.FromSeconds(
            double.TryParse(Environment.GetEnvironmentVariable("STS2_COMMAND_TIMEOUT"), out var secs) && secs > 0 ? secs : 30);
        var loop = new EngineLoop(timeout);
        var engineThread = new Thread(loop.Run) { IsBackground = true, Name = "sts2-engine" };
        engineThread.Start();
        loop.Watch(engineThread);
    }

    // RunSimulator is only touched from methods JIT-compiled after the assembly resolver is set up.
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static void EnableDebugCommands() => RunSimulator.DebugCommands = true;

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static bool DebugTraces() => RunSimulator.DebugTraces;

    static bool IsQuit(JsonElement cmd) =>
        cmd.ValueKind == JsonValueKind.Object && cmd.TryGetProperty("cmd", out var c)
        && c.ValueKind == JsonValueKind.String && c.GetString() == "quit";

    /// <summary>
    /// The engine thread's read/execute/write loop plus the watchdog that runs on the main
    /// thread. The watchdog never sits on the per-command path: it only polls a timestamp.
    /// </summary>
    private sealed class EngineLoop
    {
        private const int Idle = 0, Running = 1, TimedOut = 2;
        private int _state = Idle;
        private long _started;          // Stopwatch timestamp of the running work item
        private long _deadlineTicks;    // its allowed duration, in Stopwatch ticks
        private string _what = "";      // the command line (for the log)
        private readonly TimeSpan _timeout;
        private Exception? _warmupError;  // reported by the first start_run/load_save, like lazy init did

        public EngineLoop(TimeSpan timeout) => _timeout = timeout;

        private static bool IsRunStart(JsonElement cmd) =>
            cmd.ValueKind == JsonValueKind.Object && cmd.TryGetProperty("cmd", out var c)
            && c.ValueKind == JsonValueKind.String && c.GetString() is "start_run" or "load_save";

        private void Begin(string what, TimeSpan limit)
        {
            _what = what;
            Volatile.Write(ref _deadlineTicks, (long)(limit.TotalSeconds * System.Diagnostics.Stopwatch.Frequency));
            Volatile.Write(ref _started, System.Diagnostics.Stopwatch.GetTimestamp());
            Volatile.Write(ref _state, Running);
        }

        /// <summary>False if the watchdog already gave up on this work item (it then owns stdin/stdout).</summary>
        private bool End() => Interlocked.CompareExchange(ref _state, Idle, Running) == Running;

        public void Run()
        {
            // Constructing the simulator and the one-time engine warm-up get a generous
            // deadline of their own; a command read meanwhile simply waits in the pipe.
            var initLimit = _timeout > TimeSpan.FromMinutes(5) ? _timeout : TimeSpan.FromMinutes(5);
            Begin("<startup>", initLimit);
            RunSimulator sim;
            try { sim = new RunSimulator(); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[FATAL] RunSimulator init: {ex}");
                if (End()) WriteLine(new Dictionary<string, object?> { ["type"] = "fatal", ["message"] = $"{ex.GetType().Name}: {ex.Message}", ["stack_trace"] = ex.ToString() });
                Environment.Exit(1);
                return;
            }
            if (!End()) Park();
            var ready = new Dictionary<string, object?> { ["type"] = "ready", ["version"] = "0.2.0", ["localization"] = DataDirs.LocalizationFound };
            if (!DataDirs.LocalizationFound)
            {
                ready["warning"] = $"Localization tables not found (searched: {DataDirs.SearchedRoots("localization_eng")}); names and descriptions will be raw keys. Set STS2_DATA_DIR to the folder containing localization_eng/.";
                Console.Error.WriteLine($"[WARN] {ready["warning"]}");
            }
            WriteLine(ready);

            // Do the one-time engine initialisation (Harmony patches, localization, ModelDb, ...)
            // now instead of inside the client's first start_run/load_save. It has no inputs,
            // so the result is identical; a command sent meanwhile waits for it.
            if (Environment.GetEnvironmentVariable("STS2_NO_WARMUP") != "1")
            {
                Begin("<warmup>", initLimit);
                try { RunSimulator.Warmup(); }
                catch (Exception ex)
                {
                    // Lazily, this exception was the first start_run/load_save's error response;
                    // keep it that way rather than letting that command run on a half-initialised engine.
                    Console.Error.WriteLine($"[WARN] warmup: {ex}");
                    _warmupError = ex;
                }
                if (!End()) Park();
            }

            string? line;
            while ((line = Console.ReadLine()) != null)
            {
                line = line.Trim();
                if (string.IsNullOrEmpty(line)) continue;

                JsonElement cmd;
                try { cmd = JsonSerializer.Deserialize<JsonElement>(line); }
                catch (JsonException ex)
                {
                    WriteLine(sim.CompleteError("", BadRequest($"Invalid JSON: {ex.Message}")));
                    continue;
                }
                if (cmd.ValueKind != JsonValueKind.Object || !cmd.TryGetProperty("cmd", out var cmdProp)
                    || cmdProp.ValueKind != JsonValueKind.String)
                {
                    WriteLine(sim.CompleteError("", BadRequest("Each line must be a JSON object with a string 'cmd', e.g. {\"cmd\":\"get_state\"}. " +
                                                               $"Commands: {string.Join(", ", Commands)}")));
                    continue;
                }
                var cmdName = cmdProp.GetString() ?? "";

                Dictionary<string, object?>? result;
                Begin(line, _timeout);
                try
                {
                    if (_warmupError != null && IsRunStart(cmd))
                    {
                        var ex = _warmupError;
                        _warmupError = null;
                        throw new InvalidOperationException($"Engine initialisation failed: {ex.GetType().Name}: {ex.Message}", ex);
                    }
                    sim.BeforeCommand(cmdName);
                    result = HandleCommand(sim, cmd);
                    if (result != null)
                    {
                        LegalActions.Attach(result);
                        result = sim.AfterCommand(cmdName, result);
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[ERROR] {line}: {ex}");
                    result = new Dictionary<string, object?> { ["type"] = "error", ["code"] = "exception", ["message"] = $"{ex.GetType().Name}: {ex.Message}" };
                    if (DebugTraces()) result["stack_trace"] = ex.ToString();
                }
                // Every error has a code and, while a run is active, its decision and legal_actions.
                if (result != null && result.GetValueOrDefault("type") as string == "error")
                {
                    try { result = sim.CompleteError(cmdName, result); }
                    catch (Exception ex) { Console.Error.WriteLine($"[WARN] CompleteError: {ex.Message}"); }
                }
                RunSimulator.AfterEveryCommand();
                if (!End()) Park();  // too late: the client already got engine_timeout

                if (result != null)
                {
                    WriteLine(result);
                    if (result.TryGetValue("type", out var resultTypeObj) &&
                        string.Equals(resultTypeObj as string, "quit_result", StringComparison.Ordinal))
                        return;
                }
            }
        }

        // A work item finished after the watchdog answered for it. Stay off stdin/stdout for good.
        private static void Park() => Thread.Sleep(Timeout.Infinite);

        /// <summary>Main thread: wait for the engine thread to exit, answering for it if it wedges.</summary>
        public void Watch(Thread engineThread)
        {
            var pollMs = (int)Math.Clamp(_timeout.TotalMilliseconds / 10, 5, 100);
            while (true)
            {
                var wait = pollMs;
                if (Volatile.Read(ref _state) == Running)
                {
                    var elapsed = System.Diagnostics.Stopwatch.GetTimestamp() - Volatile.Read(ref _started);
                    var left = Volatile.Read(ref _deadlineTicks) - elapsed;
                    if (left <= 0)
                    {
                        if (Interlocked.CompareExchange(ref _state, TimedOut, Running) == Running)
                        {
                            Wedged();
                            return;
                        }
                        continue;
                    }
                    wait = (int)Math.Min(pollMs, left * 1000 / System.Diagnostics.Stopwatch.Frequency + 1);
                }
                if (engineThread.Join(wait)) return;
            }
        }

        private void Wedged()
        {
            var startup = _what.StartsWith('<');
            if (startup)
                Console.Error.WriteLine($"[ERROR] Engine {_what.Trim('<', '>')} did not finish; refusing commands");
            else
            {
                Console.Error.WriteLine($"[ERROR] Command timed out after {_timeout.TotalSeconds}s: {_what}");
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "engine_timeout",
                    ["message"] = $"Command did not finish within {_timeout.TotalSeconds}s; the engine is stuck. Restart the process.",
                });
            }
            // The engine thread is stuck inside a work item, not in ReadLine, so stdin is ours now.
            string? line;
            while ((line = Console.ReadLine()) != null)
            {
                line = line.Trim();
                if (string.IsNullOrEmpty(line)) continue;
                JsonElement cmd;
                try { cmd = JsonSerializer.Deserialize<JsonElement>(line); }
                catch (JsonException ex)
                {
                    WriteLine(BadRequest($"Invalid JSON: {ex.Message}"));
                    continue;
                }
                if (IsQuit(cmd))
                {
                    WriteLine(new Dictionary<string, object?> { ["type"] = "quit_result", ["success"] = true, ["save"] = null });
                    Environment.Exit(0);  // the engine thread is stuck; don't wait for it
                }
                WriteLine(new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "restart_required",
                    ["message"] = "A previous command timed out and the engine is stuck; restart the process.",
                });
            }
        }
    }

    static readonly string[] Commands =
    {
        "start_run", "action", "get_state", "get_deck", "get_relics", "get_potions", "get_piles", "get_card",
        "get_map", "load_save", "write_continue_save", "help", "quit",
    };

    static readonly HashSet<string> DebugOnly = new() { "set_player", "enter_room", "set_draw_order", "__debug_sleep" };

    static Dictionary<string, object?> BadRequest(string message) =>
        new() { ["type"] = "error", ["code"] = "bad_request", ["message"] = message };

    static string Kind(JsonElement v) => v.ValueKind switch
    {
        JsonValueKind.Number => "a number", JsonValueKind.String => "a string", JsonValueKind.Array => "an array",
        JsonValueKind.Object => "an object", JsonValueKind.True or JsonValueKind.False => "a boolean", _ => v.ValueKind.ToString(),
    };

    /// <summary>An optional string field: absent/null is null; any other type is a bad_request naming the field.</summary>
    static string? Str(JsonElement cmd, string name, out Dictionary<string, object?>? error)
    {
        error = null;
        if (!cmd.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null) return null;
        if (v.ValueKind != JsonValueKind.String) { error = BadRequest($"'{name}' must be a string, got {Kind(v)}"); return null; }
        return v.GetString();
    }

    /// <summary>An optional boolean field (absent/null = false).</summary>
    static bool Bool(JsonElement cmd, string name, out Dictionary<string, object?>? error)
    {
        error = null;
        if (!cmd.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null) return false;
        if (v.ValueKind is JsonValueKind.True or JsonValueKind.False) return v.ValueKind == JsonValueKind.True;
        error = BadRequest($"'{name}' must be true or false, got {Kind(v)}");
        return false;
    }

    /// <summary>An optional list of strings.</summary>
    static List<string>? StrList(JsonElement cmd, string name, out Dictionary<string, object?>? error)
    {
        error = null;
        if (!cmd.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null) return null;
        if (v.ValueKind != JsonValueKind.Array) { error = BadRequest($"'{name}' must be an array of strings, got {Kind(v)}"); return null; }
        var list = new List<string>();
        foreach (var e in v.EnumerateArray())
        {
            if (e.ValueKind != JsonValueKind.String) { error = BadRequest($"'{name}' must be an array of strings, got an element that is {Kind(e)}"); return null; }
            list.Add(e.GetString() ?? "");
        }
        return list;
    }

    /// <summary>Text language: the game's two localizations here (default en).</summary>
    static string Lang(JsonElement cmd, out Dictionary<string, object?>? error)
    {
        var lang = Str(cmd, "lang", out error);
        if (error != null || lang == null) return "en";
        if (lang is "en" or "zh") return lang;
        error = BadRequest($"'lang' must be \"en\" or \"zh\", got \"{lang}\"");
        return "en";
    }

    static Dictionary<string, object?>? HandleCommand(RunSimulator sim, JsonElement cmd)
    {
        var cmdType = cmd.GetProperty("cmd").GetString() ?? "";
        if (DebugOnly.Contains(cmdType) && !RunSimulator.DebugCommands)
            return new Dictionary<string, object?>
            {
                ["type"] = "error", ["code"] = "debug_disabled",
                ["message"] = $"'{cmdType}' is a debug command (it changes or reveals state a player can't); " +
                              "start the engine with STS2_DEBUG=1 or --debug to use it.",
            };
        switch (cmdType)
        {
            case "start_run":
            {
                var ascension = 0;
                if (cmd.TryGetProperty("ascension", out var asc) && asc.ValueKind != JsonValueKind.Null)
                {
                    if (asc.ValueKind != JsonValueKind.Number || !asc.TryGetInt32(out ascension) || ascension < 0 || ascension > 10)
                        return BadRequest($"'ascension' must be an integer 0-10, got {asc.GetRawText()}");
                }
                // The character is the player's pick: the character select highlights the first button, but a run
                // starts only on Embark (NCharacterSelectScreen.OnEmbarkPressed), so none is assumed here.
                var character = Str(cmd, "character", out var e1); if (e1 != null) return e1;
                if (string.IsNullOrWhiteSpace(character))
                    return BadRequest("start_run requires character: Ironclad|Silent|Defect|Regent|Necrobinder");
                // A missing/null/blank seed is a random one (reported in context.seed).
                var seed = Str(cmd, "seed", out var e2); if (e2 != null) return e2;
                var lang = Lang(cmd, out var e3); if (e3 != null) return e3;
                var omitDeck = Bool(cmd, "omit_deck", out var e4); if (e4 != null) return e4;
                var manualRewards = Bool(cmd, "manual_rewards", out var e5); if (e5 != null) return e5;
                var modifiers = StrList(cmd, "modifiers", out var e6); if (e6 != null) return e6;
                var gameMode = Str(cmd, "game_mode", out var e7); if (e7 != null) return e7;
                var profile = Str(cmd, "profile", out var e8); if (e8 != null) return e8;
                sim.OmitDeck = omitDeck;
                return sim.StartRun(character, ascension, seed, lang, manualRewards, modifiers, gameMode, profile);
            }

            case "action":
            {
                if (!cmd.TryGetProperty("action", out var actElem) || actElem.ValueKind != JsonValueKind.String)
                    return BadRequest("'action' must be a string, e.g. {\"cmd\":\"action\",\"action\":\"end_turn\"}; " +
                                      "send one entry of legal_actions");
                var action = actElem.GetString() ?? "";
                Dictionary<string, object?>? actionArgs = null;
                if (cmd.TryGetProperty("args", out var argsElem) && argsElem.ValueKind != JsonValueKind.Null)
                {
                    if (argsElem.ValueKind != JsonValueKind.Object)
                        return BadRequest("'args' must be an object, e.g. {\"card_index\":0}");
                    actionArgs = new Dictionary<string, object?>();
                    foreach (var prop in argsElem.EnumerateObject())
                    {
                        // null means "not given" (e.g. target_index: null on a card without a target).
                        if (prop.Value.ValueKind == JsonValueKind.Null) continue;
                        actionArgs[prop.Name] = prop.Value.ValueKind switch
                        {
                            // Non-integers are kept as doubles so validation can name the bad arg.
                            JsonValueKind.Number => prop.Value.TryGetInt32(out var iv) ? (object)iv : prop.Value.GetDouble(),
                            JsonValueKind.String => prop.Value.GetString(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            // [0, 2] -> "0,2", the form list-valued args (indices) use.
                            JsonValueKind.Array => string.Join(",", prop.Value.EnumerateArray().Select(v => v.ToString())),
                            _ => prop.Value.GetRawText(),
                        };
                    }
                }
                return sim.Act(action, actionArgs);
            }

            case "load_save":
            {
                var savePath = Str(cmd, "path", out var e1); if (e1 != null) return e1;
                // 'json' is the save file's text (a string), as written by write_continue_save.
                var saveJson = Str(cmd, "json", out var e2); if (e2 != null) return e2;
                var loadLang = Lang(cmd, out var e3); if (e3 != null) return e3;
                var omitDeck = Bool(cmd, "omit_deck", out var e4); if (e4 != null) return e4;
                var manualRewards = Bool(cmd, "manual_rewards", out var e5); if (e5 != null) return e5;
                var profile = Str(cmd, "profile", out var e6); if (e6 != null) return e6;
                if (saveJson == null && savePath != null)
                {
                    try { saveJson = File.ReadAllText(savePath); }
                    catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or NotSupportedException or ArgumentException)
                    {
                        return new Dictionary<string, object?>
                        {
                            ["type"] = "error", ["code"] = "io_error",
                            ["message"] = ex is FileNotFoundException or DirectoryNotFoundException
                                ? $"Save file not found: {savePath}" : $"Cannot read save file '{savePath}': {ex.GetType().Name}: {ex.Message}",
                        };
                    }
                }
                if (saveJson == null)
                    return BadRequest("Provide 'path' or 'json' for load_save");
                sim.OmitDeck = omitDeck;
                return sim.LoadSave(saveJson, loadLang, manualRewards, profile);
            }
            case "get_map":
                return sim.GetFullMap();

            case "get_state":
            case "state":
                return sim.GetState();

            case "get_deck":
            {
                var upgrades = Bool(cmd, "upgrades", out var ue); if (ue != null) return ue;
                return sim.GetDeck(upgrades);
            }

            case "get_relics":
                return sim.GetRelics();

            case "get_potions":
                return sim.GetPotions();

            case "get_piles":
            {
                var pile = Str(cmd, "pile", out var pe); if (pe != null) return pe;
                return sim.GetPiles(pile?.ToLowerInvariant());
            }

            case "get_card":
            {
                var id = Str(cmd, "id", out var ie); if (ie != null) return ie;
                string? where = null; int? index = null;
                foreach (var (key, loc) in new[] { ("deck_index", "deck"), ("hand_index", "hand"), ("index", "deck") })
                {
                    if (!cmd.TryGetProperty(key, out var iv)) continue;
                    if (iv.ValueKind != JsonValueKind.Number || !iv.TryGetInt32(out var n))
                        return BadRequest($"'{key}' must be an integer");
                    where = loc; index = n;
                    break;
                }
                if (id == null && index == null)
                    return BadRequest("get_card needs deck_index, hand_index or id");
                var upgraded = Bool(cmd, "upgraded", out var ug); if (ug != null) return ug;
                return sim.GetCard(where, index, id, upgraded);
            }

            case "help":
                return RunSimulator.Help();

            case "__debug_sleep":
                // Test hook for the command watchdog: blocks the engine thread.
                Thread.Sleep(cmd.TryGetProperty("ms", out var ms) && ms.TryGetInt32(out var msv) ? msv : 60000);
                return new Dictionary<string, object?> { ["type"] = "ok" };

            case "__debug_stats" when Environment.GetEnvironmentVariable("STS2_DEBUG_COMMANDS") == "1":
                return RunSimulator.DebugStats();

            case "set_player":
            {
                var args = new Dictionary<string, JsonElement>();
                foreach (var prop in cmd.EnumerateObject())
                    if (prop.Name != "cmd") args[prop.Name] = prop.Value;
                return sim.SetPlayer(args);
            }

            case "enter_room":
            {
                var roomType = Str(cmd, "type", out var e1) ?? ""; if (e1 != null) return e1;
                var encounter = Str(cmd, "encounter", out var e2); if (e2 != null) return e2;
                var eventId = Str(cmd, "event", out var e3); if (e3 != null) return e3;
                return sim.EnterRoom(roomType, encounter, eventId);
            }

            case "set_draw_order":
            {
                var cards = StrList(cmd, "cards", out var ce); if (ce != null) return ce;
                return sim.SetDrawOrder(cards ?? new List<string>());
            }

            case "write_continue_save":
            {
                var outputPath = Str(cmd, "path", out var pe); if (pe != null) return pe;
                return sim.SaveCheckpoint(outputPath);
            }

            case "quit":
            {
                var outputPath = Str(cmd, "path", out var pe); if (pe != null) return pe;
                if (!string.IsNullOrEmpty(outputPath))
                {
                    var saveResult = sim.SaveCheckpoint(outputPath);
                    bool saveOk = saveResult.TryGetValue("success", out var sObj) && sObj is bool b && b;
                    if (!saveOk)
                    {
                        // Save failed — do NOT clean up so the caller can retry with a different path.
                        return new Dictionary<string, object?>
                        {
                            ["type"] = "save_error",
                            ["save"] = saveResult,
                        };
                    }
                    sim.CleanUp();
                    return new Dictionary<string, object?>
                    {
                        ["type"] = "quit_result",
                        ["success"] = true,
                        ["save"] = saveResult,
                    };
                }
                sim.CleanUp();
                return new Dictionary<string, object?>
                {
                    ["type"] = "quit_result",
                    ["success"] = true,
                    ["save"] = null,
                };
            }

            default:
                return new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "unknown_command",
                    ["message"] = $"Unknown command '{cmdType}'. Commands: {string.Join(", ", Commands)}",
                };
        }
    }

    static void WriteLine(Dictionary<string, object?> data)
    {
        lock (ProtocolLock)
        {
            ProtocolBuf.Clear();
            var w = ProtocolJson ??= new Utf8JsonWriter(ProtocolBuf, new JsonWriterOptions { Encoder = JsonOpts.Encoder });
            w.Reset(ProtocolBuf);
            try { JsonSerializer.Serialize(w, data, JsonOpts); }
            finally { w.Flush(); }
            System.Buffers.BuffersExtensions.Write(ProtocolBuf, "\n"u8);
            ProtocolOut.Write(ProtocolBuf.WrittenSpan);
            ProtocolOut.Flush();
            // Don't keep a multi-MB buffer alive after one huge response.
            if (ProtocolBuf.Capacity > 1 << 22) ProtocolBuf = new(1 << 16);
        }
    }
}
