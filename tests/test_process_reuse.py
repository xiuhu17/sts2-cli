"""Reusing one engine process across runs, and state after background-flow prompts."""


def _play(game, state, steps):
    """Play with HeuristicPolicy('reuse') (python/policy.py), seeded per call so two calls
    on the same run make the same choices; returns the list of states seen."""
    policy = game.policy("reuse")
    seen = [state]
    for _ in range(steps):
        d = state.get("decision")
        if d in (None, "game_over"):
            break
        state = game.step(state, policy)
        seen.append(state)
    return seen


class TestProcessReuse:
    def test_start_run_twice(self, game):
        first = game.start(seed="reuse-a")
        assert first["decision"] == "event_choice"
        second = game.start(character="Silent", seed="reuse-b")
        assert second["type"] == "decision", second
        assert second["decision"] == "event_choice"
        assert "Silent" in second["player"]["name"]

    def test_same_seed_replays_identically_in_one_process(self, game):
        a = _play(game, game.start(seed="reuse-det"), 60)
        b = _play(game, game.start(seed="reuse-det"), 60)
        assert len(a) > 10
        assert a == b

    def test_new_run_while_prompt_pending(self, game):
        state = game.start(seed="bench_7")
        # Neow option 0 for this seed opens a card selection.
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_select"
        fresh = game.start(seed="reuse-after-prompt")
        assert fresh["type"] == "decision", fresh
        assert fresh["decision"] == "event_choice"
        assert _play(game, fresh, 30)[-1]["type"] == "decision"


class TestStateAfterSelection:
    def test_event_card_select_returns_current_state(self, game):
        """After resolving an event's card selection the event has finished; the response
        is its final page (finished, a single Proceed option), not the stale option page."""
        state = game.start(seed="bench_7")
        assert state["event_name"] == "Neow"
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "card_select"
        state = game.act("select_cards", indices="0")
        assert state["decision"] == "event_choice" and state.get("finished") is True, state.get("decision")
        assert [o.get("is_proceed") for o in state["options"]] == [True]
        state = game.act("choose_option", option_index=0)
        assert state["decision"] == "map_select", state.get("decision")


class TestResetValidation:
    def test_bad_character_keeps_current_run(self, game):
        state = game.start(seed="keep-run")
        err = game.start(character="Watcher", seed="x")
        assert err["type"] == "error"
        # The original run is still playable.
        opt = next(o for o in state["options"] if not o.get("is_locked"))
        after = game.act("choose_option", option_index=opt["index"])
        assert after["type"] == "decision", after

    def test_bad_save_keeps_current_run(self, game):
        game.start(seed="keep-run-2")
        err = game.send({"cmd": "load_save", "json": "{not json"})
        assert err["type"] == "error"
        assert game.get_map()["type"] == "map"
