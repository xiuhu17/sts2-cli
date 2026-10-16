"""Combat info/action parity with the real game: what the CLI shows and accepts in combat."""

import re


def _combat(game, character="Ironclad", deck=None, encounter="SHRINKER_BEETLE_WEAK", seed="cp1",
            potions=None, relics=None, hp=None, lang="en"):
    game.start(character=character, seed=seed, lang=lang)
    kw = {}
    if deck:
        kw["deck"] = deck
    if potions:
        kw["potions"] = potions
    if relics:
        kw["relics"] = relics
    if hp:
        kw["hp"] = hp
    if kw:
        r = game.set_player(**kw)
        assert r.get("type") == "ok", r
    state = game.enter_room("combat", encounter=encounter)
    assert state["decision"] == "combat_play", state
    return state


def _card(state, name):
    return next(c for c in state["hand"] if c["name"] == name)


class TestCardCosts:
    def test_void_form_makes_star_cards_playable(self, game):
        state = _combat(game, "Regent", ["VOID_FORM"] + ["FALLING_STAR"] * 4)
        fs = _card(state, "Falling Star")
        assert fs["star_cost"] == 2 and fs["can_play"] is True
        state = game.act("play_card", card_index=fs["index"], target_index=0)
        # 1 star left: Falling Star (2 stars) can't be played, and says why.
        fs = _card(state, "Falling Star")
        assert fs["can_play"] is False and fs["unplayable_reasons"] == ["StarCostTooHigh"]
        state = game.act("play_card", card_index=_card(state, "Void Form")["index"])
        assert state["stars"] == 1
        for c in state["hand"]:
            assert c["star_cost"] == 0 and c["can_play"] is True
        plays = [a for a in state["legal_actions"] if a["action"] == "play_card"]
        assert len(plays) == len(state["hand"])
        state = game.act("play_card", card_index=0, target_index=0)
        assert state["decision"] == "combat_play" and state["stars"] == 1

    def test_x_cost_flags(self, game):
        state = _combat(game, "Ironclad", ["WHIRLWIND"] * 5, encounter="NIBBITS_NORMAL")
        ww = state["hand"][0]
        assert ww["costs_x"] is True and ww["cost"] == 0 and ww["x_value"] == state["energy"]
        row = ww["damage_by_target"][0]
        assert row["repeat"] == state["energy"] and row["total_damage"] == row["damage"] * state["energy"]
        assert all(c.get("costs_x") for c in state["player"]["deck"])

    def test_star_x_card(self, game):
        state = _combat(game, "Regent", ["STARDUST"] * 5, encounter="NIBBITS_NORMAL")
        sd = state["hand"][0]
        assert sd["star_cost_x"] is True and sd["x_value"] == state["stars"]
        # Random-target attack still gets a per-enemy preview, hitting X (= stars) times.
        assert sd["damage_by_target"][0]["repeat"] == state["stars"]

    def test_star_cost_on_deck_and_rarity(self, game):
        state = _combat(game, "Regent", ["FALLING_STAR", "STRIKE_REGENT"])
        deck = {c["name"]: c for c in state["player"]["deck"]}
        assert deck["Falling Star"]["star_cost"] == 2
        assert "star_cost" not in deck["Strike"]
        assert deck["Falling Star"]["rarity"]

    def test_unplayable_reasons(self, game):
        state = _combat(game, "Ironclad", ["CLASH", "INJURY", "DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "BASH"])
        clash, injury = _card(state, "Clash"), _card(state, "Injury")
        assert clash["can_play"] is False and clash["unplayable_reasons"] == ["BlockedByCardLogic"]
        assert injury["unplayable_reasons"] == ["HasUnplayableKeyword"] and injury["unplayable"] is True
        playable = _card(state, "Defend")
        assert playable["can_play"] is True and "unplayable_reasons" not in playable
        r = game.act("play_card", card_index=clash["index"], target_index=0)
        assert r["type"] == "error" and "BlockedByCardLogic" in r["message"]


class TestUpgradedCards:
    def test_upgraded_name_in_hand(self, game):
        state = _combat(game, "Ironclad", ["ARMAMENTS", "WHIRLWIND"])
        state = game.act("play_card", card_index=_card(state, "Armaments")["index"])
        if state["decision"] == "card_select":
            state = game.act("select_cards", indices="0")
        ww = next(c for c in state["hand"] if c["id"] == "CARD.WHIRLWIND")
        assert ww["name"] == "Whirlwind+" and ww["upgraded"] is True and ww["upgrade_level"] == 1

    def test_card_reward_has_meta(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD"] * 5, hp=999)
        policy = game.policy()
        for _ in range(60):
            if state["decision"] != "combat_play":
                break
            state = game.step(state, policy)
        if state["decision"] == "card_reward":
            for c in state["cards"]:
                assert "upgraded" in c and "rarity" in c
                assert c["name"].endswith("+") == c["upgraded"]


class TestEnemies:
    def test_reviving_segment_and_stable_ids(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD"] * 5, encounter="DECIMILLIPEDE_ELITE",
                        potions=["FIRE_POTION"] * 3)
        ids = [e["combat_id"] for e in state["enemies"]]
        assert len(set(ids)) == 3 and all(e["monster_id"] for e in state["enemies"])
        first = ids[0]
        # Hit segment 0 by id until it dies; it then shows up as a reviving inactive enemy.
        for _ in range(3):
            if first not in [e["combat_id"] for e in state["enemies"]]:
                break
            state = game.act("use_potion", potion_index=0, target_id=first)
            assert state["decision"] == "combat_play", state
        assert first not in [e["combat_id"] for e in state["enemies"]]
        inactive = state["inactive_enemies"]
        assert inactive[0]["combat_id"] == first and inactive[0]["reviving"] is True
        assert [e["index"] for e in state["enemies"]] == list(range(len(state["enemies"])))
        r = game.act("play_card", card_index=0, target_id=first)
        assert r["type"] == "error" and "target_id" in r["message"]

    def test_bad_target_index_is_an_error(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD"] * 5, encounter="NIBBITS_NORMAL",
                        potions=["FIRE_POTION"])
        r = game.act("use_potion", potion_index=0, target_index=5)
        assert r["type"] == "error" and "out of range" in r["message"]
        r = game.act("play_card", card_index=0, target_index=7)
        assert r["type"] == "error" and "out of range" in r["message"]
        state = game.act("use_potion", potion_index=0, target_id=state["enemies"][1]["combat_id"])
        assert state["decision"] == "combat_play"
        assert state["player"]["potions"] == []

    def test_kaiser_crab_facing(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD"] * 5, encounter="KAISER_CRAB_BOSS")
        assert state["player_facing"] in ("Left", "Right")
        behind = [e for e in state["enemies"] if e["behind_player"]]
        assert len(behind) == 1
        ids = {p["id"] for e in state["enemies"] for p in e["powers"]}
        assert {"BACK_ATTACK_LEFT_POWER", "BACK_ATTACK_RIGHT_POWER"} <= ids
        before = state["player_facing"]
        state = game.act("play_card", card_index=0, target_index=behind[0]["index"])
        assert state["player_facing"] != before
        assert not next(e for e in state["enemies"] if e["combat_id"] == behind[0]["combat_id"])["behind_player"]

    def test_intent_tooltips_and_status_count(self, game):
        state = _combat(game, "Ironclad", ["DEFEND_IRONCLAD"] * 5, encounter="CHOMPERS_NORMAL")
        intents = [i for e in state["enemies"] for i in (e["intents"] or [])]
        status = [i for i in intents if i["type"] == "StatusCard"]
        assert status, intents
        for i in status:
            assert i["card_count"] >= 1
            assert str(i["card_count"]) in i["description"] and "{" not in i["description"]
        assert all(i.get("title") for i in intents)

    def test_monster_and_boss_names(self, game):
        state = _combat(game, "Ironclad", ["DEFEND_IRONCLAD"] * 5, encounter="TEST_SUBJECT_BOSS")
        assert state["enemies"][0]["name"] == "Test Subject #C8"
        game.start(seed="cp-boss")
        r = game.send({"cmd": "get_map"})
        for name in ((r.get("boss") or {}).get("name"), state["context"]["boss"]["name"]):
            assert name and "{" not in name and ".name" not in name and not name.isupper()

    def test_temporary_power_names_and_types(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD"] * 5, potions=["FLEX_POTION"])
        state = game.act("use_potion", potion_index=0)
        powers = {p["id"]: p for p in state["player_powers"]}
        assert powers["FLEX_POTION_POWER"]["name"] == "Flex Potion"
        assert powers["STRENGTH_POWER"]["type"] == "Buff"
        enemy_powers = [p for e in state["enemies"] for p in (e.get("powers") or [])]
        assert all(p["id"] and p["type"] in ("Buff", "Debuff", "None") for p in enemy_powers)


class TestCharacterMechanics:
    def test_osty_powers_and_damage_preview(self, game):
        state = _combat(game, "Necrobinder", ["SIC_EM", "FLATTEN", "STRIKE_NECROBINDER", "DEFEND_NECROBINDER",
                                             "BODYGUARD"], encounter="NIBBITS_NORMAL")
        assert any(p["id"] == "DIE_FOR_YOU_POWER" for p in state["osty"]["powers"])
        sic = _card(state, "Sic 'Em")
        assert all(r["dealer"] == "osty" and r["damage"] > 0 for r in sic["damage_by_target"])
        assert "1 Energy" not in _card(state, "Flatten")["description"]

    def test_orbs_listed_when_empty(self, game):
        state = _combat(game, "Defect", ["DUALCAST"] * 5, relics=[])
        if not state["orbs"]:
            assert state["orb_slots"] >= 1
            return
        while state["orbs"]:
            assert state["orbs"][0]["evokes_next"] is True
            assert state["orbs"][0]["description"]
            dc = [c for c in state["hand"] if c["can_play"]]
            if not dc:
                break
            state = game.act("play_card", card_index=dc[0]["index"])
        assert state["orbs"] == [] and state["orb_slots"] >= 1


class TestPilesAndText:
    def test_discard_pile_in_real_order(self, game):
        state = _combat(game, "Ironclad", ["DEFEND_IRONCLAD", "STRIKE_IRONCLAD", "BASH", "INFLAME", "TRUE_GRIT"]
                        * 2, hp=999)
        played = []
        for _ in range(3):
            card = next((c for c in state["hand"] if c["can_play"] and c["type"] != "Power"), None)
            if card is None:
                break
            args = {"card_index": card["index"]}
            if card["target_type"] == "AnyEnemy":
                args["target_index"] = 0
            played.append(card["id"])
            state = game.act("play_card", **args)
            if state["decision"] == "card_select":
                state = game.select(state, 0)  # the test picks the card at index 0
        ids = [c["id"] for c in state["discard_pile"]]
        assert ids == [i for i in played if i in ids]
        assert all(c["count"] == 1 and c["description"] for c in state["discard_pile"])

    def test_draw_pile_sorted_like_game(self, game):
        state = _combat(game, "Ironclad", ["BASH", "STRIKE_IRONCLAD", "INFLAME", "DEFEND_IRONCLAD"] * 4)
        rarity = {"Basic": 0, "Common": 1, "Uncommon": 2, "Rare": 3}
        keys = [(rarity.get(c["rarity"], 9), c["id"]) for c in state["draw_pile"]]
        assert keys == sorted(keys)
        assert all(c.get("description") for c in state["draw_pile"])

    def test_energy_icon_text(self, game):
        cards = ["SUPERCRITICAL", "POUNCE", "FERAL", "ADAPTIVE_STRIKE", "CORRUPTION", "ORBIT", "STOMP"]
        for lang in ("en", "zh"):
            game.start(seed="cp-energy", lang=lang)
            r = game.set_player(deck=cards, relics=["VERY_HOT_COCOA"])
            texts = [c["description"] for c in r["player"]["deck"]]
            texts += [(c.get("after_upgrade") or {}).get("description") or "" for c in r["player"]["deck"]]
            texts += [x["description"] for x in r["player"]["relics"]]
            for t in texts:
                assert not re.search(r"\d1 Energy|\b0 1 Energy|less 1 Energy|\d1点能量|0 1点能量", t), t
            if lang == "en":
                joined = " ".join(texts)
                assert "Gain 4 Energy" in joined and "costs 0 Energy" in joined
                assert "Add a 0 Energy copy" in joined and "4 Energy" in joined

    def test_affliction_changes_card_text(self, game):
        state = _combat(game, "Ironclad", ["STRIKE_IRONCLAD", "BASH", "DEFEND_IRONCLAD"] * 3,
                        encounter="QUEEN_BOSS", hp=999)
        seen_bound = False
        for _ in range(4):
            for c in state["hand"]:
                if c.get("affliction") == "Bound":
                    seen_bound = True
                    assert c["description"].endswith("Bound"), c
            state = game.act("end_turn")
            if state["decision"] != "combat_play":
                break
        assert seen_bound


class TestPotionUse:
    def test_foul_potion_in_shop(self, game):
        game.start(seed="cp-foul")
        game.set_player(potions=["FOUL_POTION"])
        state = game.enter_room("shop")
        pot = state["player"]["potions"][0]
        assert pot["can_use"] is True
        gold = state["player"]["gold"]
        state = game.act("use_potion", potion_index=0)
        assert state["decision"] == "shop"
        assert state["player"]["gold"] == gold + 100 and state["player"]["potions"] == []

    def test_foul_potion_outside_shop_not_usable(self, game):
        game.start(seed="cp-foul2")
        r = game.set_player(potions=["FOUL_POTION"])
        assert r["player"]["potions"][0]["can_use"] is False
        r = game.act("use_potion", potion_index=0)
        assert r["type"] == "error"
        assert game.send({"cmd": "get_map"}).get("type") != "error"

    def test_combat_only_potion_outside_combat(self, game):
        # NPotionPopup.RefreshButtons: CombatOnly potions can't be used outside combat.
        game.start(seed="cp-flex-ooc")
        r = game.set_player(potions=["FLEX_POTION"])
        assert r["player"]["potions"][0]["can_use"] is False
        r = game.act("use_potion", potion_index=0)
        assert r["type"] == "error" and "combat" in r["message"]
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert state["player"]["potions"][0]["can_use"] is True
        state = game.act("use_potion", potion_index=0)
        assert state["player"]["potions"] == []

    def test_event_locks_potions(self, game):
        # Ranwid the Elder sets Player.CanUseOrRemovePotions = false: no Use, no Discard.
        game.start(seed="cp-ranwid")
        game.set_player(potions=["FLEX_POTION"])
        state = game.enter_room("event", event="RANWID_THE_ELDER")
        assert state["decision"] == "event_choice"
        assert state["player"]["can_use_or_remove_potions"] is False
        assert not [a for a in state["legal_actions"] if "potion" in a["action"]]
        r = game.act("discard_potion", potion_index=0)
        assert r["type"] == "error"


class TestShopCardMeta:
    def test_x_cost_meta_keeps_gold_price(self, game):
        # Card meta must not touch the shop's gold "cost" (energy cost is "card_cost").
        for seed in ("cp-shop1", "cp-shop2", "cp-shop3", "cp-shop4"):
            game.start(character="Ironclad", seed=seed)
            state = game.enter_room("shop")
            for c in state["cards"]:
                assert c["cost"] > 0, c
                if c.get("costs_x"):
                    assert c["card_cost"] == 0
