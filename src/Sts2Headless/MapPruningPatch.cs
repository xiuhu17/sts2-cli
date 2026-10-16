using System.Runtime.InteropServices;
using System.Text;
using HarmonyLib;
using MegaCrit.Sts2.Core.Map;

namespace Sts2Headless;

/// <summary>
/// Faster, output-identical MapPathPruning.FindMatchingSegments (the game's map generator spends
/// most of start_run / act transitions there: up to ~0.5 s on some seeds).
///
/// The game's version keys every segment of every path with a fresh StringBuilder plus
/// string.Join over a LINQ Select, slices with Skip/Take/ToArray, and keeps the keys in a
/// SortedDictionary (ordinal). This keeps the exact same keys, the same insertion order inside
/// each list and the same ordinal key order of the result (the caller shuffles each list with
/// the map RNG, so order is behaviour), but builds keys with one reused StringBuilder, uses a
/// hash map and sorts the keys once at the end.
///
/// STS2_VERIFY_MAP_PRUNING=1 runs the game's version as well and fails loudly on any difference;
/// STS2_GAME_MAP_PRUNING=1 leaves the game's version in place.
/// </summary>
internal static class MapPruningPatch
{
    private static bool s_verify;
    [ThreadStatic] private static List<List<MapPoint[]>>? s_fast;

    public static void Install(Harmony harmony)
    {
        if (Environment.GetEnvironmentVariable("STS2_GAME_MAP_PRUNING") == "1") return;
        try
        {
            var target = AccessTools.Method(typeof(MapPathPruning), nameof(MapPathPruning.FindMatchingSegments), new[] { typeof(MapPoint) });
            if (target == null || target.ReturnType != typeof(List<List<MapPoint[]>>))
            {
                Console.Error.WriteLine("[WARN] MapPathPruning.FindMatchingSegments not found; keeping the game's version");
                return;
            }
            s_verify = Environment.GetEnvironmentVariable("STS2_VERIFY_MAP_PRUNING") == "1";
            harmony.Patch(target,
                prefix: new HarmonyMethod(typeof(MapPruningPatch), nameof(Prefix)),
                postfix: s_verify ? new HarmonyMethod(typeof(MapPruningPatch), nameof(VerifyPostfix)) : null);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[WARN] Map pruning patch: {ex.Message}");
        }
    }

    private static bool Prefix(MapPoint startingMapPoint, ref List<List<MapPoint[]>> __result)
    {
        var fast = FindMatchingSegments(startingMapPoint);
        if (s_verify)
        {
            s_fast = fast;
            return true;  // run the game's version too; VerifyPostfix compares
        }
        __result = fast;
        return false;
    }

    private static void VerifyPostfix(List<List<MapPoint[]>> __result)
    {
        var fast = s_fast;
        s_fast = null;
        if (fast == null) return;
        bool same = fast.Count == __result.Count;
        for (int i = 0; same && i < fast.Count; i++)
        {
            same = fast[i].Count == __result[i].Count;
            for (int j = 0; same && j < fast[i].Count; j++)
                same = fast[i][j].AsSpan().SequenceEqual(__result[i][j]);
        }
        Interlocked.Increment(ref VerifiedCalls);
        if (!same)
            throw new InvalidOperationException("MapPruningPatch: FindMatchingSegments differs from the game's version");
    }

    public static int VerifiedCalls;

    // ─── The game's algorithm, same results ───

    public static List<List<MapPoint[]>> FindMatchingSegments(MapPoint startingMapPoint)
    {
        var paths = MapPathPruning.FindAllPaths(startingMapPoint);
        var segments = new Dictionary<string, List<MapPoint[]>>(StringComparer.Ordinal);
        var sb = new StringBuilder(64);
        foreach (var path in paths)
            AddSegments(path, segments, sb);
        var keys = new List<string>(segments.Count);
        foreach (var (key, list) in segments)
            if (list.Count > 1) keys.Add(key);
        keys.Sort(StringComparer.Ordinal);
        var result = new List<List<MapPoint[]>>(keys.Count);
        foreach (var k in keys) result.Add(segments[k]);
        return result;
    }

    private static void AddSegments(List<MapPoint> path, Dictionary<string, List<MapPoint[]>> segments, StringBuilder sb)
    {
        var span = CollectionsMarshal.AsSpan(path);
        for (int i = 0; i < span.Length - 1; i++)
        {
            var start = span[i];
            if (!(start.Children.Count > 1 || start.coord.row == 0)) continue;  // IsValidSegmentStartMapPoint
            for (int j = 2; j < span.Length - i; j++)
            {
                var end = span[i + j];
                if (end.parents.Count < 2) continue;  // IsValidSegmentEndMapPoint
                var segment = span.Slice(i, j + 1).ToArray();
                var key = Key(segment, sb);
                if (!segments.TryGetValue(key, out var list))
                    segments[key] = new List<MapPoint[]>(1) { segment };
                else if (!AnyOverlapping(list, segment))
                    list.Add(segment);
            }
        }
    }

    // GenerateSegmentKey: "{row}-{endcol},{endrow}-" for a row-0 start, else
    // "{col},{row}-{endcol},{endrow}-", then the point types joined with ",".
    private static string Key(MapPoint[] segment, StringBuilder sb)
    {
        sb.Clear();
        var first = segment[0];
        var last = segment[^1];
        if (first.coord.row == 0)
            sb.Append(first.coord.row).Append('-');
        else
            sb.Append(first.coord.col).Append(',').Append(first.coord.row).Append('-');
        sb.Append(last.coord.col).Append(',').Append(last.coord.row).Append('-');
        for (int k = 0; k < segment.Length; k++)
        {
            if (k > 0) sb.Append(',');
            sb.Append((int)segment[k].PointType);
        }
        return sb.ToString();
    }

    private static bool AnyOverlapping(List<MapPoint[]> existing, MapPoint[] segment)
    {
        foreach (var e in existing)
            if (Overlapping(e, segment)) return true;
        return false;
    }

    private static bool Overlapping(MapPoint[] a, MapPoint[] b)
    {
        if (a.Length < 3 || b.Length < 3) return false;
        for (int i = 1; i <= a.Length - 2; i++)
            if (object.Equals(a[i], b[i])) return true;
        return false;
    }
}
