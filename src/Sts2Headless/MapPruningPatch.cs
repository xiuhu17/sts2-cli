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
        var segments = new Dictionary<SegKey, List<MapPoint[]>>();
        var path = new List<MapPoint>(32);
        var done = new HashSet<MapPoint>(ReferenceEqualityComparer.Instance);
        if (!Dfs(startingMapPoint, path, done, segments, int.MaxValue))
            return SlowFindMatchingSegments(startingMapPoint);  // a path too long to pack
        // The game keeps the segments in a SortedDictionary keyed (ordinal) by
        // GenerateSegmentKey; the packed key maps 1:1 to that string, so sort by it.
        var sb = new StringBuilder(64);
        var keyed = new List<(string key, List<MapPoint[]> list)>();
        foreach (var list in segments.Values)
            if (list.Count > 1) keyed.Add((Key(list[0], sb), list));
        keyed.Sort((a, b) => string.CompareOrdinal(a.key, b.key));
        var result = new List<List<MapPoint[]>>(keyed.Count);
        foreach (var (_, list) in keyed) result.Add(list);
        return result;
    }

    /// <summary>
    /// GenerateSegmentKey's content as a value: the start (its coord, or just "row 0" for a
    /// row-0 start), the end coord and the point types (4 bits each, up to 32 points). Two
    /// segments get the same packed key exactly when they get the same string key.
    /// </summary>
    private readonly record struct SegKey(int Start, int End, int Length, ulong Types0, ulong Types1);

    private const int MaxPacked = 32;

    // Walks the paths in MapPathPruning.FindAllPaths order (children in their collection order,
    // a path ends at the Boss) without materializing them. Once a node's subtree has been fully
    // enumerated, every segment that starts at or after that node on a later path is an exact
    // copy of one already offered: its key exists and its list already holds an array it
    // overlaps (the same one, or the one that rejected it; lists only grow). Offering it again
    // is a no-op, so starts from that node on are skipped. Same result, same list order.
    // Returns false if a path is too long for the packed key.
    private static bool Dfs(MapPoint p, List<MapPoint> path, HashSet<MapPoint> done,
        Dictionary<SegKey, List<MapPoint[]>> segments, int skipFrom)
    {
        if (skipFrom == int.MaxValue && done.Contains(p)) skipFrom = path.Count;
        path.Add(p);
        if (path.Count > MaxPacked) return false;
        if (p.PointType == MapPointType.Boss)
            AddSegments(path, segments, skipFrom);
        else
            foreach (var c in p.Children)
                if (!Dfs(c, path, done, segments, skipFrom)) return false;
        path.RemoveAt(path.Count - 1);
        done.Add(p);
        return true;
    }

    private static void AddSegments(List<MapPoint> path, Dictionary<SegKey, List<MapPoint[]>> segments, int skipFrom)
    {
        var span = CollectionsMarshal.AsSpan(path);
        for (int i = 0; i < span.Length - 1 && i < skipFrom; i++)
        {
            var start = span[i];
            if (!(start.Children.Count > 1 || start.coord.row == 0)) continue;  // IsValidSegmentStartMapPoint
            int startKey = start.coord.row == 0 ? -1 : (start.coord.col << 16) | (start.coord.row & 0xFFFF);
            // Types of span[i..i+j], packed incrementally (points 0..15 in t0, 16..31 in t1).
            ulong t0 = (ulong)((int)start.PointType & 0xF), t1 = 0;
            for (int j = 1; j < span.Length - i; j++)
            {
                var end = span[i + j];
                ulong t = (ulong)((int)end.PointType & 0xF);
                if (j < 16) t0 |= t << (4 * j); else t1 |= t << (4 * (j - 16));
                if (j < 2 || end.parents.Count < 2) continue;  // IsValidSegmentEndMapPoint
                var segment = span.Slice(i, j + 1);
                var key = new SegKey(startKey, (end.coord.col << 16) | (end.coord.row & 0xFFFF), j + 1, t0, t1);
                // Arrays are only allocated for segments that are actually stored.
                if (!segments.TryGetValue(key, out var list))
                    segments[key] = new List<MapPoint[]>(1) { segment.ToArray() };
                else if (!AnyOverlapping(list, segment))
                    list.Add(segment.ToArray());
            }
        }
    }

    // The straightforward version (string keys over materialized paths), for paths longer than
    // the packed key holds; no real map gets there.
    private static List<List<MapPoint[]>> SlowFindMatchingSegments(MapPoint startingMapPoint)
    {
        var segments = new SortedDictionary<string, List<MapPoint[]>>(StringComparer.Ordinal);
        var sb = new StringBuilder(64);
        foreach (var path in MapPathPruning.FindAllPaths(startingMapPoint))
        {
            var span = CollectionsMarshal.AsSpan(path);
            for (int i = 0; i < span.Length - 1; i++)
            {
                if (!(span[i].Children.Count > 1 || span[i].coord.row == 0)) continue;
                for (int j = 2; j < span.Length - i; j++)
                {
                    if (span[i + j].parents.Count < 2) continue;
                    var segment = span.Slice(i, j + 1).ToArray();
                    var key = Key(segment, sb);
                    if (!segments.TryGetValue(key, out var list))
                        segments[key] = new List<MapPoint[]>(1) { segment };
                    else if (!AnyOverlapping(list, segment))
                        list.Add(segment);
                }
            }
        }
        return segments.Values.Where(l => l.Count > 1).ToList();
    }

    // GenerateSegmentKey: "{row}-{endcol},{endrow}-" for a row-0 start, else
    // "{col},{row}-{endcol},{endrow}-", then the point types joined with ",".
    private static string Key(ReadOnlySpan<MapPoint> segment, StringBuilder sb)
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

    private static bool AnyOverlapping(List<MapPoint[]> existing, ReadOnlySpan<MapPoint> segment)
    {
        foreach (var e in existing)
            if (Overlapping(e, segment)) return true;
        return false;
    }

    private static bool Overlapping(MapPoint[] a, ReadOnlySpan<MapPoint> b)
    {
        if (a.Length < 3 || b.Length < 3) return false;
        for (int i = 1; i <= a.Length - 2; i++)
            if (object.Equals(a[i], b[i])) return true;
        return false;
    }
}
