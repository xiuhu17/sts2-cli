"""Room parity with the real game: events, shop, rest site, rewards, game over."""
import re

import pytest

ZERO_NAME = re.compile(r"\b0 to your Deck|with 0\b|into 0\b|the 0\b|Obtain 0\b|Procure 0\b|Gold\. 0\b")


def fight(game, state, prefer=None, max_steps=400):
    """Play the first affordable card (preferring ids containing `prefer`) until combat ends."""
    for _ in range(max_steps):
        if state.get("decision") != "combat_play":
            return state
        hand = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
        if prefer:
            hand.sort(key=lambda c: 0 if prefer in c["id"] else 1)
        if hand:
            args = {"card_index": hand[0]["index"]}
            if hand[0].get("target_type") == "AnyEnemy":
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
        else:
            state = game.act("end_turn")
    raise RuntimeError("combat did not end")


def actions(state):
    return [a["action"] for a in state.get("legal_actions", [])]


class TestEventText:
    @pytest.mark.parametrize("event", ["GRAVE_OF_THE_FORGOTTEN", "TEA_MASTER", "WOOD_CARVINGS",
                                       "DROWNING_BEACON", "SUNKEN_STATUE", "SYMBIOTE", "OROBAS",
                                       "DARV", "WELCOME_TO_WONGOS", "LOST_WISP"])
    def test_string_vars_are_names_not_zero(self, game, event):
        """StringVars (relic/card/enchantment names) are filled in as NEventOptionButton does."""
        game.skip_neow(game.start(seed="evt_" + event))
        game.set_player(gold=300)
        state = game.enter_room("event", event=event)
        assert state["decision"] == "event_choice", state
        for opt in state["options"]:
            text = f'{opt["title"]} | {opt.get("description") or ""}'
            assert "{" not in text, text
            assert not ZERO_NAME.search(text), text
            for v in (opt.get("vars") or {}).values():
                assert isinstance(v, (int, float, str))

    def test_option_hover_tips_and_flags(self, game):
        game.skip_neow(game.start(seed="tips1"))
        state = game.enter_room("event", event="SUNKEN_STATUE")
        sword = state["options"][0]
        assert "Sword of Stone" in sword["description"]
        assert any(t["title"] == "Sword of Stone" for t in sword["tips"])
        for opt in state["options"]:
            assert opt["is_proceed"] is False
            assert isinstance(opt["will_kill"], bool)

    def test_lethal_option_is_flagged(self, game):
        game.skip_neow(game.start(seed="tips2"))
        game.set_player(hp=2)
        state = game.enter_room("event", event="DENSE_VEGETATION")
        trudge = next(o for o in state["options"] if "HP" in (o.get("description") or "") and "Lose" in o["description"])
        assert trudge["will_kill"] is True

    def test_card_preview_tip(self, game):
        game.skip_neow(game.start(seed="tips3"))
        state = game.enter_room("event", event="BUGSLAYER")
        tip = state["options"][0]["tips"][0]
        assert tip["card"].startswith("CARD.") and tip["description"]

    def test_keyword_tips_follow_language_in_a_reused_process(self, game):
        # The game caches keyword tips process-wide; an en run must not leak into a zh one.
        for lang in ("en", "zh"):
            game.skip_neow(game.start(seed="tips4", lang=lang))
            state = game.enter_room("event", event="TEA_MASTER")
            titles = [t["title"] for o in state["options"] for t in (o.get("tips") or [])]
        assert titles and not any(re.fullmatch(r"[A-Za-z ]+", t) for t in titles), titles

    def test_bbcode_with_parameters_is_stripped(self, game):
        game.skip_neow(game.start(seed="bb1"))
        state = game.enter_room("event", event="AROMA_OF_CHAOS")
        assert "[" not in state["description"] and "rainbow" not in state["description"]

    def test_ancient_relic_option_uses_event_description(self, game):
        """Relic options fall back to the relic's eventDescription with its vars (loc keys
        are checked truthfully, so the game's fallbacks apply)."""
        state = game.start(seed="a5")
        for opt in state["options"]:
            assert opt["relic"]["id"] and opt["relic"]["rarity"]
            assert "{" not in (opt["description"] or "")

    def test_neow_dialogue(self, game):
        state = game.start(seed="dlg1")
        assert state["dialogue"] and state["dialogue"][0]["speaker"] == "ancient"
        assert state["dialogue"][0]["text"] and "[" not in state["dialogue"][0]["text"]
        zh = game.start(seed="dlg1", lang="zh")
        assert zh["dialogue"][0]["text"] != state["dialogue"][0]["text"]


class TestEventFinalPage:
    def test_outcome_page_then_proceed(self, game):
        game.skip_neow(game.start(seed="fin1"))
        state = game.enter_room("event", event="BYRDONIS_NEST")
        max_hp = state["player"]["max_hp"]
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "event_choice" and state["finished"] is True
        assert state["description"]  # what happened
        assert state["player"]["max_hp"] > max_hp
        assert [o["is_proceed"] for o in state["options"]] == [True]
        assert game.act("choose_option", option_index=1)["type"] == "error"
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select"

    def test_cannot_leave_an_unfinished_event(self, game):
        game.skip_neow(game.start(seed="fin2"))
        game.enter_room("event", event="BYRDONIS_NEST")
        assert game.act("leave_room")["type"] == "error"


class TestShop:
    def test_card_removal_once_per_visit(self, game):
        game.skip_neow(game.start(seed="a1"))
        game.set_player(gold=400)
        state = game.enter_room("shop")
        assert state["card_removal_available"] is True
        cost = state["card_removal_cost"]
        state = game.act("remove_card")
        assert state["decision"] == "card_select"
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "shop"
        assert state["card_removal_available"] is False
        assert "remove_card" not in actions(state)
        assert game.act("remove_card")["type"] == "error"
        # The next merchant charges more (MerchantCardRemovalEntry.PriceIncrease).
        game.act("leave_room")
        state = game.enter_room("shop")
        assert state["card_removal_available"] is True
        assert state["card_removal_cost"] == cost + 25

    def test_item_ids_rarity_tips_and_sold_out(self, game):
        game.skip_neow(game.start(seed="shopid"))
        game.set_player(gold=999)
        state = game.enter_room("shop")
        for r in state["relics"]:
            assert r["id"] and r["rarity"] and r["name"]
        for p in state["potions"]:
            assert p["id"] and p["rarity"] and p["usage"] and p["target_type"]
        for c in state["cards"]:
            assert c["id"].startswith("CARD.")
        assert any(c["colorless"] for c in state["cards"]) and not state["cards"][0]["colorless"]
        state = game.act("buy_potion", potion_index=0)
        sold = state["potions"][0]
        assert sold["is_stocked"] is False and sold["name"] is None and sold["description"] is None


class TestRestSite:
    def test_options_have_text_and_heal_amount(self, game):
        game.skip_neow(game.start(seed="rs1"))
        game.set_player(hp=30, relics=["REGAL_PILLOW"])
        state = game.enter_room("rest_site")
        heal = next(o for o in state["options"] if o["option_id"] == "HEAL")
        assert heal["name"] == "Rest" and "Regal Pillow" in heal["description"]
        assert heal["heal_amount"] == int(state["player"]["max_hp"] * 0.3) + 15
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        assert smith["name"] == "Smith" and smith["description"]

    def test_miniature_tent_allows_more_options(self, game):
        game.skip_neow(game.start(seed="rs2"))
        game.set_player(hp=30, relics=["MINIATURE_TENT"])
        state = game.enter_room("rest_site")
        assert state["can_leave"] is False
        heal = next(o for o in state["options"] if o["option_id"] == "HEAL")
        state = game.act("choose_option", option_index=heal["index"])
        assert state["decision"] == "rest_site"
        assert [o["option_id"] for o in state["options"]] == ["SMITH"]
        assert state["can_leave"] is True and "leave_room" in actions(state)
        state = game.act("leave_room")
        assert state["decision"] == "map_select"

    def test_without_tent_one_option(self, game):
        game.skip_neow(game.start(seed="rs3"))
        game.set_player(hp=30)
        state = game.enter_room("rest_site")
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select"

    def test_cannot_leave_before_a_choice(self, game):
        game.skip_neow(game.start(seed="rs4"))
        state = game.enter_room("rest_site")
        assert "leave_room" not in actions(state)
        assert game.act("leave_room")["type"] == "error"
        # Cancelling Smith keeps the rest site open, still without Proceed.
        smith = next(o for o in state["options"] if o["option_id"] == "SMITH")
        state = game.act("choose_option", option_index=smith["index"])
        assert state["decision"] == "card_select"
        state = game.act("skip_select")
        assert state["decision"] == "rest_site" and state["can_leave"] is False


class TestEventCombatResume:
    def test_battleworn_dummy_returns_to_the_event(self, game):
        # The fight resumes its event (shouldResumeAfterCombat): outcome page and potion reward.
        game.skip_neow(game.start(seed="dum1"))
        game.send({"cmd": "set_player", "hp": 999, "max_hp": 999, "deck": ["BLUDGEON"] * 10})
        state = game.enter_room("event", event="BATTLEWORN_DUMMY")
        state = fight(game, game.act("choose_option", option_index=0))
        for _ in range(5):
            if state["decision"] != "card_reward":
                break
            state = game.act("skip_card_reward")
        assert state["decision"] == "event_choice" and state["event_id"] == "BATTLEWORN_DUMMY"
        assert state["finished"] is True and state["description"]
        assert len(state["player"]["potions"]) == 1
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select"


class TestCardRewardAlternatives:
    def test_driftwood_reroll(self, game):
        game.skip_neow(game.start(seed="cr1"))
        game.set_player(relics=["DRIFTWOOD"], hp=80)
        state = fight(game, game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"))
        assert state["decision"] == "card_reward" and state["from_event"] is False
        ids = [a["id"] for a in state["alternatives"]]
        assert ids == ["Skip", "REROLL"] and state["can_skip"] is True
        before = [c["id"] for c in state["cards"]]
        reroll = next(a["index"] for a in state["alternatives"] if a["id"] == "REROLL")
        assert {"action": "select_card_alternative", "args": {"alternative_index": reroll}} in state["legal_actions"]
        state = game.act("select_card_alternative", alternative_index=reroll)
        assert state["decision"] == "card_reward"
        assert [c["id"] for c in state["cards"]] != before

    def test_manual_skip_keeps_reward_and_sacrifice(self, game):
        game.skip_neow(game.send({"cmd": "start_run", "character": "Ironclad", "seed": "cr2",
                                  "manual_rewards": True}))
        game.set_player(relics=["PAELS_WING"], hp=80)
        state = fight(game, game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"))
        card = next(r for r in state["rewards"] if r["type"] == "card")
        state = game.act("claim_reward", reward_index=card["index"])
        assert [a["id"] for a in state["alternatives"]] == ["Skip", "SACRIFICE"]
        state = game.act("skip_card_reward")
        # Skip closes the card screen but the reward stays claimable (NRewardButton).
        assert state["decision"] == "rewards"
        assert any(r["type"] == "card" for r in state["rewards"])
        state = game.act("claim_reward", reward_index=card["index"])
        sac = next(a["index"] for a in state["alternatives"] if a["id"] == "SACRIFICE")
        state = game.act("select_card_alternative", alternative_index=sac)
        assert state["decision"] == "rewards"
        assert not any(r["type"] == "card" for r in state["rewards"])

    def test_draft_cannot_skip(self, game):
        state = game.send({"cmd": "start_run", "character": "Silent", "seed": "m1",
                           "modifiers": ["DRAFT"]})
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_reward"
        assert state["can_skip"] is False and "skip_card_reward" not in actions(state)
        assert game.act("skip_card_reward")["type"] == "error"


class TestSpecialRewards:
    def test_card_removal_reward_is_offered(self, game):
        """Forbidden Grimoire's CardRemovalReward used to be dropped with automatic rewards."""
        game.skip_neow(game.start(seed="sp1"))
        game.set_player(deck=["FORBIDDEN_GRIMOIRE"] + ["STRIKE_IRONCLAD"] * 5, hp=80)
        state = fight(game, game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"), prefer="GRIMOIRE")
        seen_removal = False
        for _ in range(10):
            if state["decision"] == "card_reward":
                state = game.act("skip_card_reward")
            elif state["decision"] == "card_select":
                seen_removal = True
                state = game.act("select_cards", indices="0")
            elif state["decision"] == "rewards":
                state = game.act("proceed")
            else:
                break
        assert seen_removal
        assert state["player"]["deck_size"] == 5

    def test_manual_reward_types(self, game):
        game.skip_neow(game.send({"cmd": "start_run", "character": "Ironclad", "seed": "sp2",
                                  "manual_rewards": True}))
        game.set_player(deck=["FORBIDDEN_GRIMOIRE"] + ["STRIKE_IRONCLAD"] * 5, hp=80)
        state = fight(game, game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK"), prefer="GRIMOIRE")
        removal = next(r for r in state["rewards"] if r["type"] == "card_removal")
        assert removal["description"]
        for r in state["rewards"]:
            if r["type"] == "potion":
                assert r["id"] and r["rarity"] and r["usage"]


class TestNestedEventRewards:
    def test_relic_offering_its_own_rewards(self, game):
        """Lost Coffer claimed from Neow's Bones opens a second reward screen; the first one
        comes back afterwards (RewardsSetSynchronizer.rewardsStack)."""
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "nb72",
                           "manual_rewards": True})
        bones = next(o for o in state["options"] if o["relic"]["id"] == "NEOWS_BONES")
        state = game.act("choose_option", option_index=bones["index"])
        ids = [r["id"] for r in state["rewards"]]
        assert "LOST_COFFER" in ids and len(ids) == 2
        other = next(i for i in ids if i != "LOST_COFFER")
        state = game.act("claim_reward", reward_index=ids.index("LOST_COFFER"))
        assert sorted(r["type"] for r in state["rewards"]) == ["card", "potion"]
        state = game.act("proceed")  # leave the coffer's rewards
        assert state["decision"] == "rewards"
        assert [r.get("id") for r in state["rewards"]] == [other]
        state = game.act("claim_reward", reward_index=state["rewards"][0]["index"])
        state = game.skip_neow(state)
        assert state["decision"] == "map_select"
        relics = [r["name"] for r in state["player"]["relics"]]
        assert "Lost Coffer" in relics
        assert game.start(seed="after-nested")["type"] == "decision"


class TestFakeMerchant:
    def test_shop_and_foul_potion_fight(self, game):
        game.skip_neow(game.start(seed="fm1"))
        game.set_player(gold=300, potions=["FOUL_POTION"], hp=80, max_hp=500)
        state = game.enter_room("event", event="FAKE_MERCHANT")
        assert state["decision"] == "shop" and state["event"] == "FAKE_MERCHANT"
        assert len(state["relics"]) == 6 and state["cards"] == []
        relic = state["relics"][0]
        state = game.act("buy_relic", relic_index=0)
        assert state["player"]["gold"] == 300 - relic["cost"]
        assert state["relics"][0]["is_stocked"] is False
        potion = state["throwable_potions"][0]
        assert {"action": "use_potion", "args": {"potion_index": potion}} in state["legal_actions"]
        state = game.act("use_potion", potion_index=potion)
        assert state["decision"] == "combat_play"
        game.set_player(hp=500)
        state = fight(game, state)
        relics = [r["name"] for r in state["player"]["relics"]]
        assert any("Rug" in r for r in relics)
        assert len(relics) >= 1 + 1 + 1 + 5

    def test_leave(self, game):
        game.skip_neow(game.start(seed="fm2"))
        game.set_player(gold=300)
        state = game.enter_room("event", event="FAKE_MERCHANT")
        assert state["decision"] == "shop"
        assert game.act("leave_room")["decision"] == "map_select"


class TestModifiers:
    def test_character_cards_needs_a_character(self, game):
        err = game.send({"cmd": "start_run", "character": "Silent", "seed": "m1",
                         "modifiers": ["CHARACTER_CARDS"]})
        assert err["type"] == "error" and "CHARACTER_CARDS:DEFECT" in err["message"]
        assert "DEPRECATED_MODIFIER" not in err["message"]
        state = game.send({"cmd": "start_run", "character": "Silent", "seed": "m1",
                           "modifiers": ["CHARACTER_CARDS:DEFECT"]})
        game.skip_neow(state)
        state = game.enter_room("shop")
        assert state["decision"] == "shop"


class TestGameOver:
    def test_run_stats(self, game):
        state = game.skip_neow(game.start(seed="go1"))
        state = game.act("select_map_node", col=state["choices"][0]["col"], row=state["choices"][0]["row"])
        game.set_player(hp=1)
        for _ in range(50):
            if state["decision"] != "combat_play":
                break
            state = game.act("end_turn")
        assert state["decision"] == "game_over" and state["victory"] is False
        assert state["total_floor"] >= 2 and isinstance(state["score"], int)
        assert state["killed_by"]["type"] == "encounter" and state["killed_by"]["name"]
        assert state["killed_by"]["quote"]


class TestCrystalSphere:
    def test_partial_items_hide_extent(self, game):
        game.skip_neow(game.start(seed="cs9"))
        game.set_player(gold=500)
        state = game.enter_room("event", event="CRYSTAL_SPHERE")
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        state = game.act("choose_option", option_index=opt["index"])
        x, y = state["hidden_cells"][len(state["hidden_cells"]) // 2]
        state = game.act("crystal_sphere_click", x=x, y=y, tool="big")
        for item in state.get("items_seen", []):
            assert item["revealed_cells"]
            if not item["fully_revealed"]:
                assert "width" not in item and "x" not in item
            else:
                assert "width" in item
