"""The game-over screen (NGameOverScreen) as text: banner, quote, score lines, badges, the
unlock score bar and the discoveries, from the engine's game_over decision. Shared by the
bin/sts2 text view (sts2_daemon) and play.py."""


def _one_line(s):
    return " ".join(str(s).split()) if s else ""


def summary_lines(st, t):
    """Lines (no colour) for what the game's game-over screen shows; t(en, zh) picks the language."""
    out = []
    if st.get("banner"):
        out.append(f"  [{st['banner']}]")
    if st.get("victory_text"):
        out.extend("  " + ln for ln in str(st["victory_text"]).splitlines() if ln.strip())
    lines = st.get("score_lines") or []
    if lines:
        width = max(len(str(ln.get("text") or "")) for ln in lines)
        for ln in lines:
            out.append(f"    {str(ln.get('text') or ''):<{width}}  {ln.get('points') or ''}")
    badges = st.get("badges") or []
    if badges:
        out.append(f"  {t('Badges', '徽章')}:")
        for b in badges:
            out.append(f"    {b.get('title')} ({b.get('rarity')}): {_one_line(b.get('description'))}")
    sp = st.get("score_progress") or {}
    # The bar (with its "Unlocks remaining" label) only shows while unlocks remain.
    if sp.get("unlocks_remaining_text") and sp.get("visible"):
        bar = sp["unlocks_remaining_text"]
        if sp.get("progress_text"):
            bar += f"  {sp['progress_text']}"
        if sp.get("unlocked"):
            ep = (sp.get("unlocked_epoch") or {}).get("title")
            bar += f"  {sp.get('unlock_text') or ''}" + (f" ({ep})" if ep else "")
        out.append("  " + bar)
    disc = st.get("discoveries") or []
    if disc:
        out.append(f"  {st.get('discoveries_header') or t('Discoveries', '发现')}:")
        for d in disc:
            names = d.get("names") or []
            shown = ", ".join(str(n) for n in names[:10]) + (", ..." if len(names) > 10 else "")
            out.append(f"    {d.get('header')}: {d.get('count')}" + (f" ({shown})" if shown and d.get("kind") != "epochs" else ""))
    return out
