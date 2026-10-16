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
    };

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
    private static TextWriter ProtocolOut = Console.Out;
    private static readonly object ProtocolLock = new();

    static void Main(string[] args)
    {
        // Assigned here, not only in the initializer: static field initializers may run
        // lazily (beforefieldinit), i.e. after SetOut below.
        ProtocolOut = Console.Out;
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

        // All game logic runs on one dedicated engine thread; this thread only reads commands and
        // waits for each result with a deadline. If the engine wedges (a game flow that never
        // completes), the client gets an engine_timeout error instead of blocking forever, and
        // the process refuses further commands so the client restarts it.
        if (args.Contains("--debug")) EnableDebugCommands();
        Serve();
    }

    // Separate from Main so nothing that references game types is JIT-compiled before the
    // assembly resolver above is installed.
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static void Serve()
    {
        var engine = new EngineThread();
        var sim = engine.Run(() => new RunSimulator(), TimeSpan.FromMinutes(5)).Result!;
        WriteLine(new Dictionary<string, object?> { ["type"] = "ready", ["version"] = "0.2.0" });

        var timeout = TimeSpan.FromSeconds(
            double.TryParse(Environment.GetEnvironmentVariable("STS2_COMMAND_TIMEOUT"), out var secs) && secs > 0 ? secs : 30);
        var wedged = false;

        string? line;
        while ((line = Console.ReadLine()) != null)
        {
            line = line.Trim();
            if (string.IsNullOrEmpty(line)) continue;

            Dictionary<string, object?>? result;
            JsonElement cmd;
            try { cmd = JsonSerializer.Deserialize<JsonElement>(line); }
            catch (JsonException ex)
            {
                WriteLine(new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Invalid JSON: {ex.Message}" });
                continue;
            }

            if (cmd.ValueKind != JsonValueKind.Object || !cmd.TryGetProperty("cmd", out var cmdProp)
                || cmdProp.ValueKind != JsonValueKind.String)
            {
                WriteLine(BadRequest("Each line must be a JSON object with a string 'cmd', e.g. {\"cmd\":\"get_state\"}. " +
                                     $"Commands: {string.Join(", ", Commands)}"));
                continue;
            }
            var cmdName = cmdProp.GetString() ?? "";

            if (wedged)
            {
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
                continue;
            }

            var job = engine.Run(() =>
            {
                sim.BeforeCommand(cmdName);
                var r = HandleCommand(sim, cmd);
                if (r != null)
                {
                    LegalActions.Attach(r);
                    r = sim.AfterCommand(cmdName, r);
                }
                return r;
            }, timeout);

            if (!job.Completed)
            {
                wedged = true;
                Console.Error.WriteLine($"[ERROR] Command timed out after {timeout.TotalSeconds}s: {line}");
                result = new Dictionary<string, object?>
                {
                    ["type"] = "error", ["code"] = "engine_timeout",
                    ["message"] = $"Command did not finish within {timeout.TotalSeconds}s; the engine is stuck. Restart the process.",
                };
            }
            else if (job.Error != null)
            {
                var ex = job.Error;
                Console.Error.WriteLine($"[ERROR] {line}: {ex}");
                result = new Dictionary<string, object?> { ["type"] = "error", ["code"] = "exception", ["message"] = $"{ex.GetType().Name}: {ex.Message}" };
                if (DebugTraces()) result["stack_trace"] = ex.ToString();
            }
            else
            {
                result = job.Result;
            }

            if (result != null)
            {
                WriteLine(result);
                if (result.TryGetValue("type", out var resultTypeObj) &&
                    string.Equals(resultTypeObj as string, "quit_result", StringComparison.Ordinal))
                {
                    break;
                }
            }
        }
    }

    // RunSimulator is only touched from methods JIT-compiled after the assembly resolver is set up.
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static void EnableDebugCommands() => RunSimulator.DebugCommands = true;

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static bool DebugTraces() => RunSimulator.DebugTraces;

    static bool IsQuit(JsonElement cmd) =>
        cmd.ValueKind == JsonValueKind.Object && cmd.TryGetProperty("cmd", out var c)
        && c.ValueKind == JsonValueKind.String && c.GetString() == "quit";

    /// <summary>A single thread that executes engine work items one at a time.</summary>
    private sealed class EngineThread
    {
        private readonly System.Collections.Concurrent.BlockingCollection<Action> _work = new();

        public EngineThread()
        {
            var t = new Thread(() => { foreach (var a in _work.GetConsumingEnumerable()) a(); })
            {
                IsBackground = true,  // never keeps the process alive
                Name = "sts2-engine",
            };
            t.Start();
        }

        public sealed class Job<T>
        {
            public bool Completed;
            public T? Result;
            public Exception? Error;
        }

        /// <summary>Run <paramref name="f"/> on the engine thread; wait at most <paramref name="timeout"/>.</summary>
        public Job<T> Run<T>(Func<T> f, TimeSpan timeout)
        {
            var job = new Job<T>();
            using var done = new ManualResetEventSlim(false);
            var signal = done;
            _work.Add(() =>
            {
                try { job.Result = f(); }
                catch (Exception ex) { job.Error = ex; }
                finally { try { signal.Set(); } catch (ObjectDisposedException) { } }
            });
            job.Completed = done.Wait(timeout);
            return job;
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

    static string? Str(JsonElement cmd, string name, out Dictionary<string, object?>? error)
    {
        error = null;
        if (!cmd.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null) return null;
        if (v.ValueKind != JsonValueKind.String) { error = BadRequest($"'{name}' must be a string"); return null; }
        return v.GetString();
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
                var character = Str(cmd, "character", out var e1); if (e1 != null) return e1;
                var seed = Str(cmd, "seed", out var e2); if (e2 != null) return e2;
                var langArg = Str(cmd, "lang", out var e3); if (e3 != null) return e3;
                sim.OmitDeck = ReadBool(cmd, "omit_deck");
                return sim.StartRun(
                    character ?? "Ironclad",
                    ascension,
                    seed,
                    langArg ?? "en",
                    ReadBool(cmd, "manual_rewards"),
                    cmd.TryGetProperty("modifiers", out var mods) && mods.ValueKind == JsonValueKind.Array
                        ? mods.EnumerateArray().Select(m => m.GetString() ?? "").ToList() : null,
                    cmd.TryGetProperty("game_mode", out var gm) ? gm.GetString() : null,
                    cmd.TryGetProperty("profile", out var prof) ? prof.GetString() : null
                );
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
                        actionArgs[prop.Name] = prop.Value.ValueKind switch
                        {
                            // Non-integers are kept as doubles so validation can name the bad arg.
                            JsonValueKind.Number => prop.Value.TryGetInt32(out var iv) ? (object)iv : prop.Value.GetDouble(),
                            JsonValueKind.String => prop.Value.GetString(),
                            JsonValueKind.True => true,
                            JsonValueKind.False => false,
                            // [0, 2] -> "0,2", the form list-valued args (indices) use.
                            JsonValueKind.Array => string.Join(",", prop.Value.EnumerateArray().Select(v => v.ToString())),
                            _ => prop.Value.ToString(),
                        };
                    }
                }
                return sim.Act(action, actionArgs);
            }

            case "load_save":
            {
                var savePath = cmd.TryGetProperty("path", out var sp) ? sp.GetString() : null;
                var saveJson = cmd.TryGetProperty("json", out var sj) ? sj.GetString() : null;
                if (saveJson == null && savePath != null)
                {
                    if (!File.Exists(savePath))
                        return new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Save file not found: {savePath}" };
                    saveJson = File.ReadAllText(savePath);
                }
                if (saveJson == null)
                    return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Provide 'path' or 'json' for load_save" };
                var loadLang = cmd.TryGetProperty("lang", out var le) ? (le.GetString() ?? "en") : "en";
                sim.OmitDeck = ReadBool(cmd, "omit_deck");
                return sim.LoadSave(saveJson, loadLang, ReadBool(cmd, "manual_rewards"));
            }
            case "get_map":
                return sim.GetFullMap();

            case "get_state":
            case "state":
                return sim.GetState();

            case "get_deck":
                return sim.GetDeck(ReadBool(cmd, "upgrades"));

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
                return sim.GetCard(where, index, id, ReadBool(cmd, "upgraded"));
            }

            case "help":
                return RunSimulator.Help();

            case "__debug_sleep":
                // Test hook for the command watchdog: blocks the engine thread.
                Thread.Sleep(cmd.TryGetProperty("ms", out var ms) ? ms.GetInt32() : 60000);
                return new Dictionary<string, object?> { ["type"] = "ok" };

            case "set_player":
            {
                var args = new Dictionary<string, JsonElement>();
                foreach (var prop in cmd.EnumerateObject())
                    if (prop.Name != "cmd") args[prop.Name] = prop.Value;
                return sim.SetPlayer(args);
            }

            case "enter_room":
            {
                var roomType = cmd.TryGetProperty("type", out var rt) ? rt.GetString() ?? "" : "";
                var encounter = cmd.TryGetProperty("encounter", out var enc) ? enc.GetString() : null;
                var eventId = cmd.TryGetProperty("event", out var ev) ? ev.GetString() : null;
                return sim.EnterRoom(roomType, encounter, eventId);
            }

            case "set_draw_order":
            {
                var cards = new List<string>();
                if (cmd.TryGetProperty("cards", out var cardsArr))
                    foreach (var c in cardsArr.EnumerateArray())
                        cards.Add(c.GetString() ?? "");
                return sim.SetDrawOrder(cards);
            }

            case "write_continue_save":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
                return sim.SaveCheckpoint(outputPath);
            }

            case "quit":
            {
                var outputPath = cmd.TryGetProperty("path", out var op) ? op.GetString() : null;
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

    static bool ReadBool(JsonElement cmd, string name) =>
        cmd.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;

    static void WriteLine(Dictionary<string, object?> data)
    {
        var json = JsonSerializer.Serialize(data, JsonOpts);
        lock (ProtocolLock)
        {
            ProtocolOut.WriteLine(json);
            ProtocolOut.Flush();
        }
    }
}
