"""The hand in the order the real game shows it, and combat event text as the game shows it.

NPlayerHand lifts the cards picked in a hand selection (CardSelectCmd.FromHand /
FromHandForUpgrade: Armaments, Touch of Insanity, Dual Wield, ...) out of the hand row and
re-adds them with NPlayerHand.Add, at the END of the row, once the card or potion that asked
finishes (OnSelectModeSourceFinished). The Hand pile itself is not reordered. The CLI shows
and indexes the hand in the row's order.
"""

import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PARITY_PROFILE = os.environ.get("STS2_PARITY_PROFILE", "")


def _combat(game, deck, character="Ironclad", seed="ho1", encounter="SHRINKER_BEETLE_WEAK", **kw):
    game.start(character=character, seed=seed)
    r = game.set_player(deck=deck, hp=999, max_hp=999, **kw)
    assert r.get("type") == "ok", r
    state = game.enter_room("combat", encounter=encounter)
    assert state["decision"] == "combat_play", state
    return state


def _ids(cards):
    return [c["id"] for c in cards]


def _replay(game, upto=None):
    """Replay the recorded real-game session (seed R3B0F9WFJUMG, Ironclad, the recorded
    profile). The recorder skipped the CLI-only 'proceed' after some screens: when an action
    is refused and proceed is the only button, press it and send the action again."""
    with open(os.path.join(FIXTURES, "r3b0f9wfjumg_gui_actions.jsonl"), encoding="utf-8") as f:
        acts = [json.loads(line) for line in f if line.strip()]
    first = dict(acts[0], profile=PARITY_PROFILE)
    state = game.send(first)
    out = [state]
    for a in acts[1:upto]:
        r = game.send(a)
        if r.get("type") == "error":
            legal = {x["action"] for x in state.get("legal_actions") or []} - {"discard_potion", "use_potion"}
            if legal == {"proceed"}:
                state = game.act("proceed")
                r = game.send(a)
        assert r.get("type") != "error", (len(out), a, r.get("message"))
        state = r
        out.append(r)
    return out


@pytest.mark.skipif(not os.path.isfile(PARITY_PROFILE), reason="STS2_PARITY_PROFILE (the recorded real-game profile) not set")
class TestRecordedRealGame:
    def test_hand_order_and_names_match_the_real_game(self, game):
        steps = _replay(game)
        names = lambda s: [c["name"] for c in s["hand"]]  # noqa: E731

        # Act 1 floor 12: Armaments upgrades Strike from [Defend, Perfected Strike, Strike, Defend]
        # (four upgradable cards: the upgrade screen opens) -> the upgraded card returns at the end.
        assert [c["name"] for c in steps[92]["cards"]] == ["防御", "完美打击", "打击", "防御"]
        assert names(steps[93]) == ["防御", "完美打击", "防御", "打击+"]

        # Act 2 floor 8 (Ovicopter): the Tough Eggs hatch into Hatchlings; every later event
        # names them as the enemies list does.
        assert [e["name"] for e in steps[250]["enemies"]][:3] == ["幼虫"] * 3
        later = json.dumps([s.get("events") for s in steps[250:254]], ensure_ascii=False)
        assert "结实的卵" not in later
        died = [e["enemy"] for e in steps[253]["events"] if e["type"] == "enemy_died"]
        assert died.count("幼虫") == 3

        # Act 2 floor 10 (Louse Progenitor): Touch of Insanity on 痛击+ (Bash+); in the real game's
        # hand afterwards 痛击+ is last.
        assert [c["name"] for c in steps[261]["cards"]] == ["打击", "痛击+", "焚烧", "撕裂", "双重打击"]
        after = steps[262]
        assert names(after) == ["打击", "焚烧", "撕裂", "双重打击", "痛击+"]
        # Indices are the shown ones: H4 is Bash+ (free this combat).
        bash = after["hand"][4]
        assert bash["cost"] == 0
        r = game.act("play_card", card_index=4, target_index=0)
        assert r.get("type") == "decision", r
        assert any(e["type"] == "card_played" and e["card"] == "痛击+" for e in r["events"]), r["events"]


class TestHandSelectionOrder:
    def test_armaments_upgraded_card_goes_to_the_end(self, game):
        state = _combat(game, ["ARMAMENTS", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH", "INFLAME"])
        hand = state["hand"]
        arm = next(c for c in hand if c["id"] == "CARD.ARMAMENTS")
        state = game.act("play_card", card_index=arm["index"])
        assert state["decision"] == "card_select"
        rest = [c for c in hand if c is not arm]
        # The upgrade screen lists the hand in the shown order.
        assert _ids(state["cards"]) == _ids(rest)
        picked = state["cards"][0]["id"]
        state = game.select(state, 0)
        assert _ids(state["hand"]) == [i for i in _ids(rest) if i != picked] + [picked]
        assert state["hand"][-1]["upgraded"] is True
        # play_card indexes the shown order.
        last = state["hand"][-1]
        args = {"card_index": last["index"]}
        if last["target_type"] == "AnyEnemy":
            args["target_index"] = 0
        r = game.act("play_card", **args)
        played = [e["card"] for e in r["events"] if e["type"] == "card_played"]
        assert played == [last["name"]], r["events"]

    def test_single_upgradable_card_keeps_its_slot(self, game):
        # One upgradable card: FromHandForUpgrade upgrades it without opening the screen, so
        # NPlayerHand never lifts it.
        state = _combat(game, ["ARMAMENTS", "STRIKE_IRONCLAD+", "BASH", "DEFEND_IRONCLAD+", "INFLAME+"])
        before = [i for i in _ids(state["hand"]) if i != "CARD.ARMAMENTS"]
        arm = next(c for c in state["hand"] if c["id"] == "CARD.ARMAMENTS")
        state = game.act("play_card", card_index=arm["index"])
        assert state["decision"] == "combat_play"
        assert _ids(state["hand"]) == before
        assert next(c for c in state["hand"] if c["id"] == "CARD.BASH")["upgraded"] is True

    def test_dual_wield_copy_then_original(self, game):
        # Dual Wield adds the copy while the original is lifted; the original comes back
        # after it, when Dual Wield finishes.
        state = _combat(game, ["DUAL_WIELD", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH", "INFLAME"])
        dw = next(c for c in state["hand"] if c["id"] == "CARD.DUAL_WIELD")
        rest = [c["id"] for c in state["hand"] if c is not dw]
        state = game.act("play_card", card_index=dw["index"])
        assert state["decision"] == "card_select"
        picked = state["cards"][0]["id"]
        assert rest.index(picked) < len(rest) - 1, "the test needs a picked card that is not already last"
        state = game.select(state, 0)
        others = list(rest)
        others.remove(picked)
        assert _ids(state["hand"]) == others + [picked, picked]


class TestCardAddedPile:
    def test_blade_dance_with_a_full_hand(self, game):
        state = _combat(game, ["BLADE_DANCE"] * 8, character="Silent", seed="sweep11")
        seen = []
        for _ in range(3):
            bd = next(c["index"] for c in state["hand"] if c["name"] == "Blade Dance")
            state = game.act("play_card", card_index=bd)
            seen += [e for e in state["events"] if e["type"] in ("card_added", "thought")]
        added = [e for e in seen if e["type"] == "card_added"]
        assert len(added) == 9 and all(e["card"] == "Shiv" for e in added)
        assert all(e["pile"] in ("hand", "discard") for e in added)
        full = [e for e in added if e["pile"] == "discard"]
        assert full and all(e.get("redirected") == "hand_full" for e in full)
        assert state["discard_pile_count"] >= len(full)
        thoughts = [e for e in seen if e["type"] == "thought"]
        assert len(thoughts) == len(full)
        assert all(t["text"] == "I have too many cards..." and t["speaker"] == "The Silent"
                   and t["key"] == "combat_messages.HAND_FULL" for t in thoughts)

    def test_anger_copy_goes_to_discard_infernal_blade_to_hand(self, game):
        state = _combat(game, ["ANGER"] * 3 + ["INFERNAL_BLADE"] * 2, seed="sweep12")
        anger = next(c for c in state["hand"] if c["name"] == "Anger")
        state = game.act("play_card", card_index=anger["index"], target_index=0)
        added = [e for e in state["events"] if e["type"] == "card_added"]
        assert added == [{"type": "card_added", "card": "Anger", "pile": "discard"}], added
        ib = next(c for c in state["hand"] if c["name"] == "Infernal Blade")
        state = game.act("play_card", card_index=ib["index"])
        added = [e for e in state["events"] if e["type"] == "card_added"]
        assert len(added) == 1 and added[0]["pile"] == "hand" and "redirected" not in added[0], added


class TestBubbles:
    @pytest.mark.parametrize("lang,start,done", [
        ("en", "Make your choice.", "It is done."),
        ("zh", "做出选择。", "此事已成。"),
    ])
    def test_knowledge_demon_lines(self, game, lang, start, done):
        game.start(seed="sweep3", lang=lang)
        game.set_player(hp=999, max_hp=999)
        state = game.enter_room("elite", encounter="KNOWLEDGE_DEMON_BOSS", act=2)
        state = game.act("end_turn")
        assert state["decision"] == "card_select"
        speech = [e for e in state["events"] if e["type"] == "speech"]
        assert [e["text"] for e in speech] == [start]
        assert speech[0]["key"] == "monsters.KNOWLEDGE_DEMON.moves.CURSE_OF_KNOWLEDGE.startLine"
        state = game.select(state, 0)
        assert [e["text"] for e in state["events"] if e["type"] == "speech"] == [done]
