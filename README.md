# sts2-cli

<details open>
<summary><b>English</b></summary>

A CLI for Slay the Spire 2.

Runs the real game engine headless in your terminal — all damage, card effects, enemy AI, relics, and RNG are identical to the actual game. Everything is unlocked from the start: all characters, cards, relics, potions, and ascension levels — no timeline progression required.

![demo](docs/demo_en.gif)

## Setup

Requirements:
- [Slay the Spire 2](https://store.steampowered.com/app/2868840/Slay_the_Spire_2/) on Steam
- [.NET 9+ SDK](https://dotnet.microsoft.com/download)
- Python 3.9+

```bash
git clone https://github.com/wuhao21/sts2-cli.git
cd sts2-cli
./setup.sh      # copies DLLs from Steam → IL patches + UI guard → builds (Debug + ReadyToRun)
```

Or just run `python3 python/play.py` — it auto-detects and sets up on first run.

Tested with **v0.111.0 (Steam public-beta, build 24724944)**. After updating the
installed game in Steam, rerun `./setup.sh` to refresh the engine DLLs, patches,
and official English/Chinese localization, then rebuild. Other game versions
may require adapter changes.

For a compatibility check, run `python3 python/play_full_run.py 5 Ironclad`
(repeat for Silent, Defect, Regent, and Necrobinder). A completed run reaches
victory or defeat; crashes, stalls, and timeouts return a nonzero exit code.

### Bots decide through a stated policy

No client chooses for the player. The bots (`play_full_run.py`, `play.py --auto`,
`agent/auto_play.py`, `agent/combat_helper.py`, `scripts/fuzz_legal.py`, `scripts/bench.py`)
pick one entry of the engine's `legal_actions` through a policy in `python/policy.py`, never
"the first option": `random` (default for `play_full_run.py --policy`: seeded, weighted by verb,
uniform within a verb, never discards potions) or `heuristic` (used by `play.py --auto`: plays a
card while one is legal and aims at the lowest-HP enemy, rests below 70% HP else smiths,
removes Strike/Defend first, claims every reward then proceeds, otherwise random). The
docstring of `python/policy.py` lists every rule. A run is reproducible from (seed, policy).
Rewards screens and treasure chests are always decisions: the clients handle `rewards` /
`treasure_relic` (they still send `"manual_rewards": true`, which the engine now ignores).

## Play

```bash
python3 python/play.py                        # interactive (Chinese)
python3 python/play.py --lang en              # interactive (English)
python3 python/play.py --ascension 10         # Ascension 10
python3 python/play.py --character Silent      # play as Silent
```

Without `--character` it asks which character to play, and again for each new run (the game's
character select only starts a run when you press Embark on a character); `--auto` and
non-interactive use need `--character`.

Type `help` in-game:

```
  help     — show help
  map      — show map
  deck     — show deck
  potions  — show potions
  relics   — show relics
  piles    — show draw / discard / exhaust piles (in combat)
  quit     — quit

  Map:     enter path number (0, 1, 2)
  Combat:  card index / e (end turn) / p0 (use potion)
  Reward:  card index / s (skip, when the reward can be skipped) / a0 (alternative)
  Rewards: index to claim / p (proceed; unclaimed rewards are left behind)
  Chest:   relic index / s (skip)
  Select:  card index(es) / s (skip or cancel, when the screen allows it)
  Rest:    option index / leave (only when the game allows it)
  Event:   option index / leave (only when the event offers no choice)
  Shop:    c0 (card) / r0 (relic) / p0 (potion) / rm (remove) / leave
  Potions: u0 use outside combat (when usable there) / x0 discard, at any prompt
```

Every prompt is built from the engine's `legal_actions`: it accepts exactly what the game
allows on that screen and nothing is chosen for you (no automatic proceed/leave after an
error; an error re-shows the same decision). A decision the client has no screen for is
shown with all its fields and its legal actions as `a0`, `a1`, ...

## For AI agents: the `sts2` shell client (recommended)

An LLM agent that plays through a Bash tool should use `bin/sts2`: one short command
per step, a compact text view with numbered legal actions, and all rules text kept.
The first call starts a background daemon that holds one engine process (unix socket
under `$TMPDIR`; restarted and the run replayed if the engine dies; stopped after 30
minutes idle, `STS2_IDLE_TIMEOUT`), so every later call is a ~20 ms round-trip
(15 ms of it is Python start-up; measured on an M-series Mac).

```bash
bin/sts2 start --character Ironclad --seed abc        # --character required; [--ascension N] [--lang zh] [--profile PATH]
bin/sts2 state                     # current decision + numbered actions
bin/sts2 do 3                      # take action [3]
bin/sts2 do "play Bash E0; play Strike E0; end"   # a chain: H#/E#/P# and numbers mean the view you
                                   # saw; stops at the first error, a new room/turn/fight end, or a
                                   # step that does not fit (see "do chains" below)
bin/sts2 do "play Body Slam -> Jaw Worm"   # multi-word names; identical cards are one play, but
                                   # differing same-name cards / same-name enemies need H<n> / E<n>
bin/sts2 deck | relics | potions | piles [draw] | map | card N | log [N]
bin/sts2 --json state              # raw engine JSON instead of text
bin/sts2 help                      # all verbs (printed locally; starts no daemon)
bin/sts2 stop [--forget]           # --forget also ends the run (else the next call replays it)
```

Each reply starts with what happened since the previous decision (cards played and
drawn, damage, block, powers, enemy moves, relic triggers, gold/deck/relic changes),
then the decision: in combat the enemies with intents and powers, the hand with card
text, energy, orbs/stars/Osty, piles; for card selections the purpose
(upgrade/remove/transform/enchant/discard/exhaust/choose), the game's prompt, the source,
whether it can be cancelled, and upgrade/enchant/transform previews; Ancient dialogue,
per-target damage (also with one enemy), the game's intent titles, the score and cause of
death at game over, a used card removal, and the Crystal Sphere grid with axes, legend and
tool rules. The rewards view says when every reward must be taken (no proceed) and labels
`proceed` as skipping the rewards not claimed; a card reward's Skip is shown as the skip
action, not as an alternative (`alt N` on it skips); unplayable hand cards say why
(`[unplayable: I don't have enough Energy]`) and potions read `[targets one enemy, combat
only]`. A card selection says how many cards it takes and from where (`select 2 cards | from
Neow | cards in your deck`, `select 0 or 2 cards`); X-star cards read `(0 X*)`; a glowing hand
card says so (`[glows gold: its condition is met]`); the map says when you may ignore paths
(the game's text; such moves are tagged `(off path)`); a returned stolen card is a reward by its name. No
view prints `None` (`remove a card` outside a shop has no price). Fields the renderer does
not know are printed as `key: value`. Replies stay
under 8 KB and the legal actions are never cut (long selections are folded into one
`[a-b]` line, the card list is shortened instead). The header line shows the act, floor,
room, boss, **ascension and seed** (also on the game-over screen). Each block of events is
printed once: a `state` right after a `do` does not repeat what the `do` reply already
showed (`sts2 log` does). Damage lines say which number is which: `Toadpole -> The Ironclad
took 12 damage (12 more blocked)` (12 HP lost, 12 absorbed by Block), `... blocked all 6
damage`; block lines read `The Ironclad gained 5 Block`. `sts2 map` numbers the columns
(COL) on top and the rows (ROW, with their floor `f`) on the left, marks where you are
(`<x>`), lists the legal moves as `go COL ROW = room type`, and tags each row that has one;
the map decision's actions are typeable as shown (`go 3 2 Unknown` -> `sts2 do "go 3 2"`). `click X Y`
needs the tool (`big` = 3x3, `small` = one cell). A card name matches every card in hand
that has it (or starts with it), playable or not: `play Def` with an unplayable Defend and a
playable Deflect is an ambiguity error listing both, never the other card, and naming a card
that can't be played is an error saying why. Commands reject arguments they don't take
(`log x`, `map extra`: usage and exit code 1). The start command is stored with the seed
the engine used (also when you gave none) in `$TMPDIR`, so a crashed engine, an engine that
died while idle, or a killed daemon replays the very same run; a replay that does not end
where the run was is reported as diverged. A daemon that stops (`sts2 stop`, idle timeout)
removes its socket, lock and empty log from `$TMPDIR` and keeps only the run file, so the
next call picks the run up again; `sts2 stop --forget` removes that too. `--lang zh` uses the game's official
Chinese text. Debug commands are never enabled in the daemon. Set `STS2_SESSION` to run
several independent games.

**`do` chains and indices.** Prefer card names (`play Strike`, `play Bash Toadpole`):
H#/E# renumber whenever a card leaves the hand or an enemy dies, so after `play H0` the old
H1 is H0. To make index steps safe anyway, every index in a chain means **the view shown
when the chain was submitted** (the last `state`/`do` reply):

- `H#` / `P#`: that card / potion, found again by its instance id (`uid`) wherever it is
  now (`play H0; play H1` plays the cards shown as H0 and H1; the reply says
  `(shown as H1)` when it moved). If it has left (played, discarded, exhausted,
  transformed) the chain stops - `play H0; play H0` never plays a second, different card.
- `E#`: that enemy, by its `combat_id`; dead or untargetable -> stop.
- action numbers `N` - every one of them (`select`, `skip`, `proceed`, `leave`, `remove`,
  `end`, potions, `claim`, `buy`, ...): the action numbered N in the list printed with that
  view, remapped the same way, and only on that very screen instance (below; in a fight the
  same turn, on rewards / shop the items left of it). On the shop's removal screen,
  `skip; remove; 13` stops before `13`: `remove` opened the removal screen again, and that
  new screen was never shown.
- screen indices (`choose N`, `pick N`, `claim N`, `buy card N`, `select i,j`, `alt N`,
  `go N`): only on the very screen instance shown - the same decision, room, source,
  purpose, prompt and offered items (by `uid`) and the engine's `selection_id` (claimed
  rewards / bought items may be gone) - and while the item at that index is unchanged. A
  screen the chain has not shown you stops the chain before such a step: the card reward
  that `claim 2` opens, the card selection of `remove`, another event page, or a second
  card selection of the same type (`select 2; play True Grit+; select 0` from Burning
  Pact's exhaust screen stops before `select 0`: True Grit+'s screen was never shown).
  The stop says what differs: `it is from True Grit+ (the one shown was from Burning Pact)`,
  `the removal screen was closed and opened again (new selection) - it was not shown to
  you`, `the event moved on to another page`, other cards / options, a reward that was not
  shown. `go COL ROW` names a map node and is not an index.
- steps without an index (`end`, `skip`, `proceed`, `leave`, `remove`, `select none`,
  `go COL ROW`, card and enemy names) are read against the game where it is now:
  `claim 2; skip` skips the card reward that `claim 2` opened. `skip` on the rewards screen
  is `proceed` only as the first step of a chain; later in a chain it never leaves the
  rewards screen (that would give up the rewards not claimed): `claim 0; skip` and
  `pick 0; skip` stop before `skip`, saying what would be given up. Write `proceed` for that.
- an action number that was not in the list shown is an error (not a stop) that lists the
  legal actions where the game is now.
- `raw` / engine verbs (`play_card card_index=2`) are sent as written.

After each step the chain continues while the next step is legal and unambiguous on the
decision the game is at now, also when its type changed (`pick 1; proceed`: card reward
-> rewards; `select 1; end` after a mid-turn card selection). It stops before the next step
when the game entered a new room, a fight started or ended, a new turn began or the run
ended; when the step does not apply to the new decision (`play` on a card selection that
popped up: "you are in combat, but a card selection by Headbutt is open: finish it first");
when a `select` step's selection never opened because the game picked the cards itself
(`play Headbutt; select 0` with one card in the discard pile: "the game already picked the
cards for that selection: Headbutt: auto-selected Strike (only 1 card could be chosen)");
when a potion step would run on any screen but the very one the chain started
at (in a fight the same turn; not a screen that opened since, even one that looks the same:
`skip; remove; discard P0` on the shop's removal screen stops before `discard P0`); or when
an index cannot be resolved safely (above); and at the first error.

Reward numbers are **stable**: claiming reward 0 does not renumber the others (`claim 0;
claim 1` claims what was shown as 0 and 1), unlike hand/enemy indices. Claiming one that
is gone says `reward 0 (8 Gold) was already claimed`. A step error is one explanation in
the output language: what each index refers to now, why the game refuses the step (said
once; the engine's flag, if any, in brackets) and the legal actions, e.g. `H2 is now Defend
(Skill, 1 energy, targets yourself) - it takes no target: play H2 without an E#. Legal now:
[1] ...`, `H1 is now Strike (...) - with several enemies it needs a target: E0 Toadpole, E1
Seapunk (e.g. play H1 E0)` or `H0 is now True Grit+ (...) - not playable: I don't have enough
Energy (EnergyCostTooHigh)` (also for E#, P#, select, claim, buy, pick, choose and go). Every
error message the engine has (tests/test_sts2_messages.py reads them from its C# source) has
such a text in English and Chinese, matched case-insensitively, and none repeats what the
index description already says: `select 0,0` -> `card 0 is Strike - card 0 is listed twice:
each card can be selected once`, `select 0` on a 2-3 card screen -> `this screen takes 2-3
cards, not 1`, `go 9 9` -> `(9,9) is outside the map (columns 0-6, rows 0-16; sts2 map)`
(the legal moves once, in the list), `alt 1` on a card reward with only its Skip button ->
`this card reward has no alternatives (its only other button is Skip): use 'skip' (action
[4]) or pick a card`. `--json` keeps the engine's raw message. They name the index you typed:
when an earlier step of the chain moved it, the message says so - `H2 (Defend as shown when
the chain started, now H1) is Defend (...)`. A screen step on a decision without that
screen says what the decision is (once) and lists the legal actions (`'go 0' picks a map
choice, but the current decision is combat_play: there is none here now. Legal now: [1]
...`); no message prints an empty range. With `--lang zh` the action labels (`[3] 打出 H1 痛击
-> E0 蟾蜍蝌蚪`, `丢弃药水 P0 火焰药水`), decision names (选牌界面, 地图, 事件页面) and
messages are Chinese; the steps you type stay as listed in `sts2 help` (`play`, `select 0,2`,
`go 3 2`), and a command shown as a hint is printed as typed (`sts2 do "select 0,2"`).

`agent/sts2_bridge.py` (HTTP, one JSON command per POST) is still available; it now
restarts a dead engine and replays the run on the seed the engine reported (a response
after a restart carries `notes`; a diverged replay says so), answers bad bodies with a JSON
error, serves `GET /state` and `GET /help`, and its `--compact` mode keeps all descriptions,
enchantments, afflictions and upgrade/enchant previews. Compact drops only: `id` / `text_key` /
`act_name` keys, null values and empty lists/objects, the full `deck` (use `get_deck`), and
`vars` when the description already has them filled in; pile lists become `"Name xN"` strings and
a card's enchantment/affliction is folded into its name (`"Strike [Sharp 2]"`). An enchant
screen's top-level `enchantment` stays an object `{name, amount, description}`.
`agent/combat_helper.py` plays only legal cards and stops at every non-combat decision; ties between equally scored cards, targets or potions are broken by a seeded RNG, not by list order.

## JSON Protocol

For programmatic control (AI agents, RL, etc.), communicate via stdin/stdout JSON:

```bash
dotnet run --project src/Sts2Headless/Sts2Headless.csproj
```

```json
{"cmd": "start_run", "character": "Ironclad", "seed": "test", "ascension": 0}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "action", "action": "end_turn"}
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}
{"cmd": "action", "action": "skip_card_reward"}
{"cmd": "quit"}
```

Each command returns a JSON decision point (`map_select` / `combat_play` / `card_reward` / `card_select` / `bundle_select` / `rest_site` / `event_choice` / `shop` / `crystal_sphere` / `rewards` / `treasure_relic` / `game_over`). All names are in English.

- **Starting a run:** `character` is required (`Ironclad`, `Silent`, `Defect`, `Regent` or `Necrobinder`): the game's character select highlights the first character when it opens, but a run only starts when the player presses Embark on one, and the protocol has no implicit Embark, so a missing or empty one is `bad_request` and an unknown one `unknown_character`. A missing, null, empty or blank `seed` is a random seed, as the game's blank seed box ("Random"); `context.seed` reports the one used. `ascension` defaults to 0 (the game's screen remembers your last level; here an unset field means 0), `lang` to `en` (`en` or `zh`, anything else is `bad_request`). Every field is type-checked: a wrong type is `bad_request` naming the field (`modifiers` is a list of strings, `manual_rewards` / `omit_deck` booleans, `profile` / `game_mode` / `seed` strings); `manual_rewards` is still accepted and ignored.
- **Many runs per process:** send `start_run` (or `load_save`) again to begin a new run; the previous one is torn down. This skips the engine start-up, so batch/RL workloads should keep one process per worker; memory stays flat over thousands of runs. `python/engine.py` has a small `Engine` wrapper (`dotnet bin/.../Sts2Headless.dll` directly, bounded reads, stderr drained).
- **Start-up:** `{"type":"ready","version":...,"localization":true}` arrives after ~40 ms; the one-time engine initialisation (~0.15 s, plus the first use of the game's save serializer, warmed on a second thread) then runs right away, so a client that takes a moment before its first `start_run` doesn't wait for it (a command sent earlier simply waits; `STS2_NO_WARMUP=1` defers it to the first `start_run`/`load_save` as before). `localization` is false (plus a `warning`) when the `localization_eng/` tables can't be found, in which case names are raw keys; they are looked up from `STS2_DATA_DIR`, then upwards from the binary, next to `lib/` (`STS2_LIB`, the lib folder the engine loaded, or `STS2_GAME_DIR`, which the launchers point at the repo's `lib/`), and upwards from the working directory; the warning lists every folder searched.
- **Builds:** `setup.sh` also publishes a ReadyToRun (precompiled) build to `src/Sts2Headless/bin/R2R/net9.0` (faster start, ~30% faster steps). `python/engine.py` launches `$STS2_ENGINE_DLL` if set, else the most recently built of `bin/R2R`, `bin/Release`, `bin/Debug` (an R2R build older than `lib/sts2.dll` is skipped), so a plain `dotnet build` after a code change is picked up automatically.
- **Null fields:** objects nested in a response (cards, relics, potions, powers, ...) omit fields whose value is null (e.g. a card without keywords has no `keywords` key); a response's own top-level fields are always present. Read optional fields with `.get(...)`.
- **Legal actions:** every decision includes `legal_actions`, a list of ready-to-send actions such as `{"action":"play_card","args":{"card_index":0,"target_index":1}}`. For multi-card prompts there is one entry `{"action":"select_cards","choose":{"from":[0,1,2],"min":1,"max":2}}`; send `{"indices":"0,2"}` (or `[0,2]`). The Crystal Sphere minigame has one entry `{"action":"crystal_sphere_click","choose_cell":{"cells":[[x,y],...],"tools":["big","small"]}}`; send `{"x":..,"y":..,"tool":..}`. In general, an entry with a `choose*` key instead of `args` needs the client to fill in the arguments. Potions whose `usage` is `Automatic` (e.g. Fairy in a Bottle) are never listed for `use_potion` and are rejected if sent. Invalid input is rejected with an error instead of being silently ignored (unreachable map nodes, locked/out-of-range options, bad card indices or counts). `scripts/fuzz_legal.py` plays random legal actions and fails on any error.
- **Strict actions:** an action that is not legal for the current decision is rejected — `{"type":"error","code":"illegal_action","message":"'leave_room' is not allowed now: a choice is still required here. Current decision: event_choice. Legal: choose_option(option_index=0|1|2)","decision":"event_choice","legal_actions":[...]}`. The check is exact: the verb **and** its arguments must match one `legal_actions` entry (an unplayable card, an out-of-range or dead target, a `target_index` on a card that doesn't target one enemy, an unaffordable item are all `illegal_action`, with the reason in the message); nothing is re-targeted or picked for you. The only argument the engine fills in is `target_index` when exactly one entry matches (a single hittable enemy). `choose` / `choose_cell` entries are checked by their handler (indices, counts, cells). Aliases for the same button: on a finished event page `leave_room` / `proceed` = its Proceed option; on `rewards` `leave_room` / `skip_rewards` = `proceed`; on `treasure_relic` `proceed` / `leave_room` = `skip_treasure_relic`; at a rest site or shop `proceed` = `leave_room`. `leave_room` can't skip events or rest sites, and `skip_select` only works when the selection is `cancelable` (the screen's cancel/skip button); a `min_select` 0 screen without one is confirmed with nothing selected by `select_cards` with `indices` `""` or `[]`. Every action error carries `decision` and `legal_actions`; unknown actions get a "did you mean" hint; an argument the verb doesn't take (`end_turn` with `{"x":1}`) is `illegal_action`, not silently ignored (the verbs' arguments are in `help`); an argument whose value is `null` counts as not given; `args` indices must be integers (numeric strings are accepted; anything else is `bad_request`), and `target_index` must name a targetable enemy (or send its `target_id`). Potions follow the game's Use button: CombatOnly potions only on your combat turn with no card selection open (outside that they are rejected instead of being lost); AnyTime potions (Blood Potion, Fruit Juice, ...) everywhere, and `legal_actions` lists them at every decision. Malformed lines (not JSON, not an object, missing `cmd`, `ascension` outside 0-10, ...) get `"code":"bad_request"`. Errors carry no .NET stack traces unless `STS2_DEBUG=1`. If a game flow started by an action fails inside the game, the reply is `"code":"flow_failed"` with the resulting decision in `state`.
- **Card selections:** `card_select` says what it is for, as the game's screen does: `purpose` (`upgrade` / `remove` / `transform` / `enchant` / `discard` / `exhaust` / `choose`), `prompt` (the game's text, e.g. "Choose a card to Exhaust."), `cancelable` (the screen has a cancel/skip button: rest-site Smith, shop removal, ...; then `skip_select` is legal and backs out), `screen` (`hand` / `deck` / `grid` / `draw` / `discard` / `exhaust` / `choose_a_card` / `reward_grid`) and `source` (`{name, kind}` of the card, relic, potion, power or event that asked). Enchant screens add `enchantment {id, name, amount, description}` and a per-card `after_enchant`; transforms with a fixed result add `transforms_to`. During combat, `combat` holds the battlefield (energy, hand, enemies with intents, powers, orbs, pile counts) like the game's peek button. Every selection the game shows a screen for is a decision, even with a single card (the engine never picks it): the game itself skips the screen only when it would (fewer cards than it must take and no manual confirmation, one upgradable card in hand, ...). Choose-a-card screens (Discovery, Abundance, potions) have `min_select` 1 and no `skip_select` unless the game shows a Skip button there (then `cancelable` is true). `min_select` / `max_select` are the selection sizes the game's screen can actually confirm, per screen class: the deck enchant screen (Kifuda, Gnarled Hammer) and the deck upgrade screen never complete with no card (their preview's Confirm does nothing with an empty selection), so Kifuda's "up to 3" is `min_select` 1; the deck transform / remove screens, the hand (Purity, Gambler's Brew, Ashwater, ...), combat-pile (Neow's Fury) and grid (Sea Glass) screens accept what their confirm button does. A screen with `min_select` 0 (Claws, Purity, Gambler's Brew, Neow's Fury, Sea Glass, ...) mirrors the game's confirm button, which is enabled with nothing selected: `legal_actions` gives `choose` with `min` 0 (a one-card screen also lists `select_cards` with `indices` `""`), and `select_cards` with `indices` `""` / `[]` confirms nothing; there is no `skip_select` unless the screen has a cancel button (`cancelable`, only the deck screens have one). A screen whose sizes have a gap (a grid that completes itself at `max_select` but can also be confirmed empty) adds `select_counts` and `choose.counts`. The engine never clamps a request to the cards offered where the game doesn't: a screen that can't be confirmed with the cards it shows (Smith asking for 2 cards with 1 upgradable card, a manual-confirm deck screen with fewer cards than `min_select`, a grid that completes itself only at `max_select`) keeps the requested `min_select` / `max_select` and has `select_counts: []`; if it is `cancelable`, `skip_select` (its Close button) is the only legal selection action and `message` says so, otherwise it is a softlock (see Softlocks). The game's own clamps are kept: no screen at all when the cards offered are at most `min_select` without manual confirmation, and the combat-pile screen (Neow's Fury) counting only the cards it shows. Debug: `set_player {"smith_count": 2}` at a rest site sets the Smith count; `set_player {"card_select": {"method": "upgrade"|"transform"|"deck"|"grid", "min": 1, "max": 3, "cancelable": false, "manual": false}}` opens a deck card selection through the game's CardSelectCmd with those prefs. Upgrade screens carry each card's `after_upgrade`; like the game's upgrade preview it upgrades a clone of the card itself, so its saved state (a played Genetic Algorithm's grown block, ...), a cost changed this turn or combat (Enlightenment, Snecko Oil, Touch of Insanity, ...) and, for a card in combat, the combat's cost and value modifiers show in it.
- **Events log:** each decision has `events`, what happened since the previous decision, in order: `turn` markers, `card_played`, `draw`, `damage` (`source`, `target`, `amount`, `blocked`, `killed`), `block`, `power`, `enemy_move` (with the intents it showed), `card_added` (with `pile`: `hand` / `draw` / `discard` / `exhaust`, where the generated card went, as the game shows it flying there; `"redirected": "hand_full"` when it was meant for a full hand and went to the discard pile; a generated card that never reaches a pile, such as Anger's copy on the blow that ends the combat, is not shown by the game and has no line), `discard`, `exhaust`, `orb_channeled`, `potion_used`, `potion_queued` (a potion used while a card's selection is still running waits until that card finishes, as in the game), `relic` (a relic triggered), `enemy_died`, `speech` / `thought` (the game's speech and thought bubbles: `speaker`, `text` in the run's language and the loc `key`, e.g. the character's "I have too many cards..." when the hand is full or "I don't have enough cards to draw.", a monster's lines such as Knowledge Demon's "Make your choice.", or "I'm blocked by Normality." when a card the game auto-plays is blocked), `card_auto_selected` (the game resolved a selection itself without opening a screen, because it could take every card it offered: Headbutt with one card in the discard pile, Armaments with one upgradable card, any selection with no more cards than its minimum; `source`, `purpose`, `screen`, `cards`, `available`, `min_select`, `prompt`), and run-level changes `gold`, `hp`, `max_hp`, `deck_added` / `deck_removed` / `deck_changed`, `relic_obtained`, `potion_obtained` / `potion_lost`. `gold` lines are one per change, in the order the game applies them: a shop purchase is `{"change": -73, "reason": "spent", "item": "Clash"}` and a relic's payout `{"change": 15, "source": "Lucky Fysh"}` (the merchant adds the card first, so Lucky Fysh's +15 comes before the price; the price charged is the `cost` the shop shows, Membership Card / The Courier discounts included); `reason` is `spent` / `lost` / `stolen`. Powers are named as the game titles them: a temporary power takes the title of the card, potion or relic that applied it (Speed Potion, Anticipate, Hotfix). Creatures are named as the enemies list names them at the time of the event (a Tough Egg that hatched is a Hatchling in its later damage and death lines). Capped at 80 entries.
- **Queries (never advance the game):** `{"cmd":"get_state"}` re-sends the current decision; `get_deck` (`"upgrades":true` adds upgrade previews), `get_relics`, `get_potions`, `get_piles` (`"pile":"draw"|"discard"|"exhaust"|"hand"`; draw pile sorted, order hidden), `get_card` (`"deck_index":N` / `"hand_index":N` / `"id":"BASH"`, `"upgraded":true`), `get_map`, `help`.
- **Lean decisions:** `"omit_deck": true` on `start_run` / `load_save` leaves `player.deck` out of every decision (`player.deck_size` and `player.deck_version`, which changes whenever the deck does, stay; `get_deck` returns the cards). Default unchanged. Responses list `type`, `decision`, `legal_actions` first, so a truncated line still shows them. Shop items also have `price` (gold) and cards `energy_cost`, since `cost` means gold there but energy everywhere else.
- **Debug commands:** `set_player`, `enter_room` and `set_draw_order` change or reveal state a player can't, so they return `"code":"debug_disabled"` unless the engine runs with `STS2_DEBUG=1`, `STS2_DEBUG_COMMANDS=1` or `--debug`. In `set_player`, a deck id with a trailing `+` (`"BASH+"`) adds an upgraded copy. `enter_room` takes an optional `"act": 1-3` that moves to that act first (RunManager.EnterAct), e.g. to fight the last act's boss.
- **Text:** every `description` (cards, relics, potions, powers, events) is the game's own text with numbers filled in, as shown in the game UI — card numbers follow live combat values (Strength, Weak, ...). Icons become text (`1 Energy`, `★`); an energy icon used as a unit after a number reads as `Energy` (`costs 0 Energy`, `Gain 4 Energy`). Cards in hand / rewards / selections / shop, potions and offered relics also carry `tips`: the secondary tooltips the game shows next to them (keyword and power explanations such as Vulnerable or Exhaust), and so do powers (player, enemies: `PowerModel.ExtraHoverTips`, e.g. Plating's Block, Vital Spark's Tainted). `tips` is exactly the game's tip set (`IHoverTip.RemoveDupes`): two tips may share a title (a Tainted card shows the Tainted affliction and the Tainted power it applies), and nothing is added the game doesn't show (a [gold] word the game gives no tip for stays as it is; Frantic Escape's "Get farther away." is plain text, its tip is Sandpit). A power's text is its smart description with the owner's context, as the hover tip renders it: entries that branch on the owner say "its turn" on an enemy (Ritual, Poison, Doom, Slow, Vigor); entries that don't (Plating) read "your turn" on an enemy too, as in the game - the power is listed under the enemy that has it.
- **Pile contents:** `combat_play` includes `draw_pile` / `discard_pile` / `exhaust_pile`, each card with its `description` and `keywords`, like the game's pile screens (the text is rendered the way the pile screen's card does: enchantments and calculated values count, e.g. Compile Driver's "(Draw 1 card)", but not Strength or other hand-only modifiers). The draw pile is grouped as `{name, count, ...}` and sorted by (rarity, id) like the game, so draw order is not revealed. The discard and exhaust piles list one entry per card (`count` 1) in their real order, as the game shows them.
- **Cards:** every card entry (hand, piles, deck, rewards, card_select, bundles, shop) is named like the game's title (`Strike+`; `Name+N` for a card that can be upgraded several times) and has `upgraded`, `upgrade_level` (when upgraded or multi-upgrade), `max_upgrade_level` (multi-upgrade cards) and `rarity`. X-energy cards have `costs_x: true` (`cost` stays 0). Cards with a star cost (Regent) have `star_cost` (0 or more), and X-star cards have `star_cost_x: true`. Cards that can never be played have `unplayable: true`. Every list (deck, `get_deck`, `get_card`, card_select of any purpose, card rewards, bundles, shop, special card rewards, card previews) shows a card the way the game's card face does: `description` and `stats` include its enchantment's numbers (Sharp 2 on Perfected Strike reads "Deal 8 damage"; Fresnel Lens's Nimble adds Block), and the card carries `enchantment` / `enchantment_amount` and `affliction` / `affliction_amount`. `after_upgrade` of an enchanted card keeps the enchantment too, and the enchant screen's `after_enchant` previews the new enchantment's own numbers. In hand, the enchantment's extra line follows live modifiers too (Adroit's "Gain 3 Block" reads 5 with 2 Dexterity), and `star_cost` includes modifiers (Void Form, Brilliant Scarf), `x_value` is what X would be if played now (the energy, or stars for X-star cards, through the game's X modifiers: Chemical X adds 2; `damage_by_target` uses the same X), and `can_play` is the game's own check. When it's false, `unplayable_reasons` lists why (`EnergyCostTooHigh`, `StarCostTooHigh`, `HasUnplayableKeyword`, `BlockedByHook`, `BlockedByCardLogic`, `NoLivingAllies`), `blocked_by` names the blocker (e.g. Normality, Smoggy), `unplayable_message` is the line the character says when you try to play it ("I don't have enough Energy.", "I'm blocked by Smoggy.", "I can't play this card."; a rejected `play_card` repeats it), and `cost_crossed: true` marks the crossed-out cost icon the game draws when the reason is not energy or stars. `glow` is `gold`/`red` when the game highlights the card. `legal_actions` follows `can_play` alone. Shop cards, relics and potions also have `id`.
- **Hand order:** `hand` (and its `index`, which `play_card` / `get_card hand_index` take, `get_piles hand` and a hand `card_select`'s `cards`) is in the order the game's hand shows the cards, left to right. That is not always the Hand pile's order: a card picked in a hand selection (Armaments' upgrade, Touch of Insanity, Dual Wield, ...) leaves the hand row while the selection is open and is added back at the right end when the card or potion that asked finishes (NPlayerHand.OnSelectModeSourceFinished); cards entering the hand are added at the right end; a card transformed in hand keeps its slot. When only one card qualifies and the game skips the screen (Armaments with a single upgradable card), nothing moves. The game's own logic (random picks, ...) still uses the pile order.
- **Enemies and targets:** `enemies` lists the targetable enemies; `index` is the `target_index` to send. Each enemy also has a `monster_id` and a `combat_id` that stays the same for the whole combat: send it as `target_id` instead of `target_index` in `play_card` / `use_potion`, so a kill mid-turn can't shift your target. Hand cards and potions have a `uid`: a per-instance id that stays the same while that card stays in hand (its `index` moves as the hand changes) or that potion stays in its slot (not an action argument; `bin/sts2` uses it to resolve H#/P# in `do` chains). The cards of `card_select` / `card_reward` carry the same `uid`, and `card_select` / `card_reward` / `bundle_select` carry `selection_id`: one id per selection screen the game opened, so two screens with the same cards (Burning Pact's exhaust, then True Grit+'s) differ. Enemies that are still in combat but can't be targeted (a Decimillipede segment waiting to reattach) appear in `inactive_enemies` with `reviving: true` and their pending move. Intents have `title` and `description` (the game's intent tooltip, e.g. "This enemy intends to give you 2 Status cards."), and Status intents have `card_count`. Powers (player, enemies, Osty) have `id`, `name`, `description`, `amount`, `type` (`Buff`/`Debuff`) and `tips`. Against the Kaiser Crab, `player_facing` is `Left`/`Right` and each crab has `behind_player` (its attacks deal x1.5; targeting it turns you around). `damage_by_target` rows have `target_id`, use CalculatedHits for `repeat`/`total_damage` (Tear Asunder, Finisher, ...), and `plays` when the card will be played more than once (Replay from Soldier's Stew or enchantments, Echo Form, Burst, Duplication, Throwing Axe, Tag Team: the game's play count), in which case `total_damage` = `damage` x `repeat` x `plays`; the hand card then also has `play_count`, and its description shows the game's "Replay N." line, and also cover Osty attacks (`dealer: "osty"`) and random-target cards. `osty` has `powers`. Defect's `orbs` / `orb_slots` are present whenever the player has orb slots (`orbs` is an empty list when none are channeled); each orb has `description` and `evokes_next` (index 0 is the rightmost orb, evoked first).
- **Custom mode:** `start_run` accepts `"modifiers": ["SEALED_DECK", "MIDAS", ...]` (an unknown name returns the list of valid ones; Character Cards names its character: `"CHARACTER_CARDS:DEFECT"`) and `"game_mode": "standard"|"custom"|"daily"` (default `custom` when modifiers are given). Deck-building modifiers (Draft, Sealed Deck, Insanity) add their Neow option as in the game. The daily challenge's seed and modifiers are generated by the game's UI from the server date and aren't reproduced; pass them explicitly with `"game_mode":"daily"`.
- **Rewards are always a decision:** nothing is collected for you, as on the game's rewards screen. After combat you get a `rewards` decision listing each reward (`gold` / `relic` / `potion` / `card` / `special_card` / `card_removal`, with a stable `index`): `claim_reward {"reward_index": i}` takes one (a card reward opens the usual `card_reward` prompt), `proceed` leaves the rest. Like the game's screen, it stays up after everything is taken: `rewards: []` with `proceed` (potions can still be used or discarded there) until you proceed. Each room gets its own rewards, also when it was entered with the debug `enter_room`. Treasure chests give their gold when opened, then a `treasure_relic` decision: `select_treasure_relic {"relic_index": i}` or `skip_treasure_relic`. Rewards handed out by events and relics (Neow's options, Cauldron, Calling Bell, Lost Coffer, ...) also arrive as a `rewards` decision with `"source":"event"`; when the game doesn't let you skip them (Neow's Bones) the decision has `"can_skip": false` and `proceed` is not offered until every reward is taken. The old `"manual_rewards"` flag of `start_run` / `load_save` is still accepted but ignored (the automatic mode was removed).
- **Profile:** `"profile": "<path to progress.save>"` on `start_run` (and `load_save`; `bin/sts2 start --profile PATH`) plays with that profile's unlocks and progress, like the game does: unlocked content, a never-discovered alternate act is forced, the first unseen boss of each act and a first-ever run's fixed Overgrowth rooms (discovery order), Neow only once its epoch is revealed, and the profile's ancient visits / Architect wins / Wongo points. The file is only read. Without a profile everything is unlocked and discovered (no discovery-order changes).
- **Real-game rules (TestMode):** the engine runs the game with its TestMode on, but every TestMode branch that changes gameplay takes the real game's path: shop potion prices get the game's ±5% jitter from the Shops RNG (so later shop rolls stay in sync too), Cauldron and Calling Bell offer random potions/relics (not the test lists), act selection and discovery-order rooms follow the profile, reward sets are never auto-claimed, Rolling Boulder hits each enemy left to right one at a time, the run history (and so `write_continue_save`) records each map point's gold/HP and a shop's unbought items, and the choose-a-bundle screen keeps the game's early outs. Each patch checks it found its TestMode branch; if the game changed, `start_run`/`load_save` return an error instead of silently playing by test rules. `STS2_TESTMODE_AUDIT=1` lists TestMode checks in game code that are not yet classified in `src/Sts2Headless/testmode-audited.txt` (`tests/test_testmode_parity.py` runs it). Debug: `set_player {"obtain_relic": "CAULDRON"}` obtains a relic with its pickup effect.
- **Crystal Sphere:** the event's minigame is a `crystal_sphere` decision (`rows` with `?` hidden / `.` empty / item letters, `hidden_cells`, `items_seen`, `divinations_left`); reveal with `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` (big = 3x3). Without `tool` the click uses the tool currently selected in the minigame (`current_tool`: the game starts on big and keeps the last one used). Each `items_seen` entry has `id`, `type`, `revealed_cells` and `fully_revealed`; its position and size (`x`/`y`/`width`/`height`) are only given once it is fully uncovered, since the fog hides the rest of the sprite.
- **Events:** `event_choice` has `event_id`, the body text in `description` (when the game shows one), and for ancients (Neow, ...) the opening `dialogue` (`[{speaker: "ancient"|"character", text, key, next}]`) until the first choice. The game draws that dialogue at random (`Rng.Chaotic`, unseeded and cosmetic) among the ones valid for the loaded profile and character (`AncientDialogueSet.GetValidDialogues`: first visit ever, the visit-index line for this character, else the repeating pool), so no exact line can be predicted: `dialogue` is a seeded pick and `dialogue_pool` lists every candidate (`[{id, lines}]`, e.g. `NEOW.talk.ANY.2-0r`), with `dialogue_random: true` when there is more than one. Each option has its text with names filled in (relics/cards/enchantments the game puts in StringVars; `vars` then holds the name as a string), `tips` (hover tips; card previews carry `card` = card id), `relic` (the relic an ancient option grants: `id`, `rarity`, ...), `will_kill` (the game flashes the option red: choosing it would kill you) and `is_proceed`. When an option finishes the event, the response is the event's last page: `"finished": true`, the outcome text in `description`, and one Proceed option; `choose_option 0` (or its aliases `leave_room` / `proceed`) then goes to the map; Neow's last page is shown the same way. `leave_room` is refused while the event is not finished. An event is never left for you.
- **Softlocks:** where the game shows nothing to click — every option of an event locked, an unfinished event with no options, a rest site whose options are all disabled before one was used, a non-cancelable card selection that can't be confirmed with the cards it shows — the decision comes back with `"softlocked": true`, `softlock_reason` and a `message`, and `legal_actions` lists only what the game's top-bar potion belt still allows there: `use_potion` for AnyTime potions with `can_use` and `discard_potion` (nothing at all when the event locks potions, like Stone of All Time); the map and deck views are information only (`get_map`, `get_deck`); the game soft-locks there too, so clients treat it as the end of the run (play_full_run, fuzz_legal, policy.py, `sts2`). The only way out is `{"cmd":"force_leave_room"}`, which is **not game behaviour**, never listed in `legal_actions`, and only works with `STS2_SOFTLOCK_ESCAPE=1` on a softlocked decision (it goes to the map; on a softlocked `card_select` it closes the selection with nothing chosen).
- **The Trial:** Double Down opens the game's abandon-run popup (NAbandonRunConfirmPopup): an `event_choice` with `popup` (`id: "abandon_run_confirm"`, `header`, `body` from main_menu_ui) and its two buttons, `0` Yes (`will_kill`) and `1` No. Yes abandons the run (RunManager.Abandon): `game_over` with `victory: false` and `abandoned: true`; No closes the popup and leaves the same page.
- **The Architect:** after the last act's boss (at Ascension 10 the second one) Proceed enters The Architect's event (RunManager.EnterNextAct), as in the game: `event_choice` with `event_id: "THE_ARCHITECT"`, its lines so far in `dialogue` and one option per line (its button text, or Respond/Continue), then Proceed, which wins the run (TheArchitect.WinRun): only then `game_over` with `victory: true`. Its dialogue follows the profile's wins for the character and the seeded event RNG, so it is exact. An event fight that returns to its event (Battleworn Dummy) goes back to it after the combat rewards: its outcome page and its own rewards (potion, relic) follow.
- **Fake Merchant:** this event is a small shop: a `shop` decision with `"event":"FAKE_MERCHANT"` (relics only; `buy_relic`, `leave_room`), and `throwable_potions` lists Foul Potions you can `use_potion` on the merchant to start the fight (its rewards include the merchant's rug and the unsold relics).
- **Shop:** relics and potions carry `id`, `rarity` and `tips` (potions also `usage` / `target_type`); cards carry `id` and `colorless` (the merchant's second row). Bought slots keep their index and have no `name`/`description` (null, so omitted like every nested null). Card removal is offered once per visit as in the game: `card_removal_available` turns false after a removal (including Lord's Parasol's free one, so no second paid removal is offered) and with modifiers that forbid it, and the next shop costs 25 more.
- **Rest site:** options have the game's `name` and `description` (heal amount after relics, extra lines such as Regal Pillow, remaining Lift uses, why an option is disabled), plus `heal_amount` for Rest. With Miniature Tent the remaining options are offered again after a choice; `can_leave` (and `leave_room`) appears once an option has been used; before that `leave_room` is refused while an option is enabled, as the game shows Proceed only after a choice. After the last option the room is not left for you: the `rest_site` decision comes back with `options: []` and `leave_room` (the Proceed button; potions can still be used). Disabled options can't be clicked (Smith with nothing to upgrade, Cook with fewer than 2 removable cards); Rest is always enabled, so a rest site only soft-locks if every option is disabled (see Softlocks).
- **Card rewards:** `card_reward` has `alternatives` (`[{index, id, name, after}]`: `Skip`, `REROLL` from Driftwood, `SACRIFICE` from Pael's Wing); `can_skip` is false when the reward can't be skipped (Draft). Pick an alternative other than Skip with `select_card_alternative {"alternative_index": i}`. `alternatives` is what the screen shows now: Driftwood rerolls once, after which only Skip is left (REROLL is gone). Pael's Wing: every second sacrifice obtains a relic (`relic_obtained` event), and its `counter` shows sacrifices toward the next one (1, then 0). Card rewards run through the game's own selection (history, hooks). Skipping a card reward (or cancelling a card removal) goes back to the `rewards` screen with it still claimable, as in the game. The prompt waits for you indefinitely, and an invalid `card_index` is an error (the prompt stays open).
- **Rewards:** reward entries carry `description`; relic rewards `id`/`rarity`, potion rewards the same fields as the potion belt. `special_card` (e.g. a card a Thieving Hopper stole, with `card`) and `card_removal` (Forbidden Grimoire) rewards are listed (claiming the removal opens a `card_select`). A potion the full belt can't take stays on the screen: `discard_potion`, then `claim_reward`, or `proceed` without it. Relics whose pickup offers its own rewards (Lost Coffer, Small Capsule, ...) open a nested `rewards` screen; the outer one comes back with what is left on it.
- **Game over:** `game_over` shows what NGameOverScreen shows. `score` (the game's ScoreUtility), `total_floor` (floors climbed over the run) and, on a defeat, `killed_by` (`type` encounter/event, `id`, `name`, `quote`: the summary page's encounter quote); `abandoned` is true for an abandoned run (The Trial's Double Down). `banner` ("Victory...?", or the random defeat banner such as "Vanquished"), `death_quote` (the first page's random quote, defeat) or `victory_text` (the Architect damage text, and the Ascension unlock line). `score_lines`: the score lines in the screen's order, each `key` (floorsClimbed, goldGained, elitesKilled, bossesSlain, ascension), `text` in the run's language ("Floors Climbed: 33"), the raw `amount` (33 floors, 511 gold, 1 elite...) and the `points` string as shown ("+490", ascension "x1.5"); the elites/bosses lines only appear when > 0, the ascension line only above Ascension 0. `badges`: the run's badges (ScoreUtility.GetBadges), each `id`, `rarity` (bronze/silver/gold), `title`, `description`. `score_progress`: the unlock score bar (AnimateScoreBar): `unlocks_remaining` and its `unlocks_remaining_text`, `visible` (false once every unlock is earned: no bar), and while visible `score_before` / `threshold`, `score_added` (this run's score), `score_after` / `threshold_after`, `progress_text` ("[310/500]"), `unlocked` with `unlock_text` and `unlocked_epoch` when the bar filled up. `discoveries_header` ("Discoveries", only when there are any) and `discoveries`: per category in the screen's order (`kind` cards, relics, potions, enemies, epochs) the `count`, the `names`, the tooltip `header` and `body` (the game's text: count line plus the first 10 names). `main_menu_button` is the bottom button's label (Unlock when an epoch was discovered). `score_breakdown` is kept for compatibility and mixes units: `floors` and `gold` are points, `elites_killed`, `bosses_slain` are counts and `ascension` the level; it also has `floors_climbed`, `floors_points`, `gold_gained`, `gold_points`, `elites_killed_points`, `bosses_slain_points` and `ascension_multiplier`. As in the game the run's end updates the profile progress in memory (discoveries, epochs, score bar); the profile file is never written. bin/sts2 and play.py print the screen.
- **Headless UI guard:** game logic that calls UI objects inline (screen shake, event-room layout, ...) would throw headless and abort halfway. Those call sites are rewritten to skip when the UI object is absent: once by `setup.sh` (`tools/UiGuardPatcher`, which stamps `lib/sts2.dll`), or, if the DLL isn't stamped, at engine start-up (~1 s per process). `STS2_NO_UI_GUARD=1` skips the start-up rewrite only (to run without any guard, put the game's `lib/sts2.dll.original` back); `STS2_UI_GUARD_RUNTIME=1` forces the start-up rewrite on an unstamped DLL and `STS2_UI_GUARD_DUMP=<file>` lists the methods it guarded (used by the tests to compare both implementations).
- **Debug hooks:** with `STS2_DEBUG_COMMANDS=1`, `{"cmd":"__debug_stats"}` returns `heap_bytes`, `combat_ended_handlers`, `net_card_db_subscriptions`, `net_loading_handles`, `save_store_calls` and `map_pruning_verified` (sizes the leak fixes keep bounded; see `tests/test_memory.py`). If start-up initialisation fails, the error is returned by the first `start_run`/`load_save`.
- **Map generation:** the game's `MapPathPruning.FindMatchingSegments` is replaced by an equivalent, faster implementation with identical results (same maps, same RNG use); `STS2_VERIFY_MAP_PRUNING=1` runs both and fails on any difference, `STS2_GAME_MAP_PRUNING=1` keeps the game's. (It walks the paths without materializing them, skips segment starts it has already seen and keys segments by value; warm `start_run` is about 2.5x faster.)
- **Animation pauses:** the game's real-time `Task.Delay` pauses in Amalgamator (300 ms between removing the two cards and adding the new one; sound and screen shake only) are skipped, like `Cmd.Wait`; the outcome is the same. `STS2_KEEP_ANIMATION_DELAYS=1` keeps them.
- **Map travel:** `map_select` offers exactly the nodes the game's map makes travelable: at the start of an act only the start (Neow / Ancient) node, then the current node's children, then the boss. While Winged Boots has charges (or with the Flight modifier) the whole next row is offered; such choices carry `"free_travel": true` (it spends a charge) and the decision has `"free_travel_available": true`. `select_map_node` is rejected anywhere but `map_select`. At Ascension 10 the last act has two bosses: after the first one the map opens for the second (`context.second_boss`), and The Architect's event (then the victory) comes only after it. Entering a new act doesn't heal; the Ancient start node heals as in the game (80% of missing HP from Ascension 2).
- **Run context:** every decision's `context` has `act`, `act_name`, `floor` (per act), `total_floor` (the top bar's floor), `room_type`, `ascension`, `seed`, `run_time_seconds` (the top bar's run timer, `RunManager.RunTime`: wall-clock, so it differs between otherwise identical runs), `modifiers` (`[{id,name}]`, only when set; named by the modifier's title like its top-bar icon, so Character Cards reads "Defect Cards" and also has `character`), `boss` and, in a double-boss act, `second_boss` (`{id, name}`, named by the encounter title like the top bar). `get_map` returns the whole graph: `start` (row 0, the root), `rows`, `boss`, `second_boss`, each node with `children`, `visited`, `current` and `travelable` (a visited `Unknown` node also has `revealed_type`, the room it turned out to be — `Monster`, `Elite`, `Treasure`, `Shop` or `Event` — which the map draws inside the `?`), plus `visited_path`, `current_coord`, `ascension_effects` (`[{level,name,description}]`), `modifiers` with descriptions and `run_time_seconds`.
- **Relics:** player relics have `id` and, like their icon in the game, `counter` (e.g. Pen Nib's attack count, Winged Boots' charges; only for relics that show one), `status` (`Active` = glowing, `Disabled` = greyed; omitted when normal), `used_up`, `melted`, `wax` and `stack_count` (only when set). Wax relics (Toy Box) are named like the game, e.g. "Wax Anchor".
- **Potions:** each potion has `can_use`: whether the game's potion popup enables Use right now. `AnyTime` potions (Blood Potion, Fruit Juice, ...) can be used outside combat and are listed as `use_potion` in `legal_actions` on any screen; Foul Potion only in combat or at the merchant (+100 gold). `CombatOnly` potions are rejected outside your combat turn and are never consumed by a failed or cancelled use. While an event locks potions (The Future of Potions, ...) `player.can_use_or_remove_potions` is false and both `use_potion` and `discard_potion` are rejected. Ranwid the Elder and Stone of All Time lock them too. A potion used while a card's selection is open (True Grit's exhaust choice, ...) is queued behind that card, as in the game: it shows `queued: true` and `can_use: false`, and neither `use_potion` nor `discard_potion` is legal for it until it has been drunk. An out-of-range `target_index` or unknown `target_id` is an error; with no target, a potion that can target you (AnyPlayer) is used on you, as in the game.
- **Saves:** `{"cmd":"write_continue_save","path":...}` / `{"cmd":"load_save","path":...}`. Saving inside a room writes what the game itself has on disk (saved on entering the node, or after the fight is won), and loading it re-enters the same node like the game's Continue: the same encounter (or the won fight's rewards) comes back, and the path can't be changed. Saving and loading again in the same room keeps doing that (the engine keeps its own copy of the loaded save, like the file on disk), boss rooms included. The saved run is frozen when the game saves: what happens afterwards in the room (e.g. a card picked from the rewards) is not in it, as in the game's file. In The Architect's room, which the game never saves (it is entered with RunManager.EnterRoom and is not an Ancient), the Continue file is the one written when the last boss died: loading it returns to the boss's finished room (its empty rewards screen) and Proceed enters The Architect again, as the game's Continue does. Saves at `map_select` restore the map position. A save the game can't build a run from (an unknown character or card id, missing players, ...) is refused with `bad_request` ("Save is not loadable: ...") before anything is torn down, so the run in progress carries on.
- **Errors:** every error is `{"type":"error","code":...,"message":...}` and, while a run is waiting on a decision, also has that `decision` and its `legal_actions` (also for query and malformed-line errors). Every input line gets exactly one reply: an empty or whitespace-only line is a `bad_request` too. Names the engine doesn't know are echoed back cut to 64 characters, and a `seed` longer than 64 characters (the game's seeds are 12) is a `bad_request`. Codes: `bad_request` (malformed line or field, wrong type, missing required field), `unknown_command`, `unknown_action`, `illegal_action` (not legal now, including a selection the handler rejects, e.g. the wrong number of cards), `unknown_character`, `no_run` (nothing started yet), `io_error` (a save/profile file that can't be read or written), `debug_disabled`, `flow_failed`, `exception`, `engine_stuck`, `engine_timeout`, `restart_required`. If the engine deadlocks and can't advance combat, or can't produce a room's rewards (combat rewards that fail to generate, a treasure relic that can't be offered or resolved), it returns `"code":"engine_stuck"` (and keeps returning it) instead of a fake defeat or silently leaving the room without them — abandon that run and start a new one.
- **Crashes:** stdout carries only protocol JSON (library/log output goes to stderr). If the process is about to die from an unhandled exception it first writes `{"type":"fatal","message":...,"stack_trace":...}`.
- `python3 scripts/bench.py [--reuse] [--jobs N] [--out f.json] [--compare base.json]` benchmarks fixed seeds and prints a trajectory fingerprint per run (null fields are ignored), plus start_run and per-step latency percentiles, for checking that engine changes don't alter game behaviour.

## Game Logs

Every run is automatically logged to `logs/` as a JSONL file (one JSON per line), recording each game state and action with timestamps. Logs older than 7 days are cleaned up automatically.

```bash
python3 python/play.py --no-log    # disable logging
```

**When filing a bug report, please attach the relevant log file from `logs/`** — it contains the full step-by-step game state needed to reproduce the issue.

## Supported Characters

| Character | Status |
|---|---|
| Ironclad | Fully playable |
| Silent | Fully playable |
| Defect | Fully playable |
| Necrobinder | Fully playable |
| Regent | Fully playable |

## Architecture

```
Your code (Python / JS / LLM)
    │  JSON stdin/stdout
    ▼
src/Sts2Headless (C#)
    │  RunSimulator.cs
    ▼
sts2.dll (game engine, IL patched + UI guard by setup.sh / tools/UiGuardPatcher)
  + src/GodotStubs (replaces GodotSharp.dll)
  + Harmony patches (localization, headless no-ops, map pruning)
```

</details>

<details>
<summary><b>中文</b></summary>

杀戮尖塔2的命令行版本。

在终端里运行真实游戏引擎 — 所有伤害计算、卡牌效果、敌人AI、遗物触发、随机数都和真实游戏一致。所有内容从一开始就全部解锁：全角色、全卡牌、全遗物、全药水、全渐进难度等级，无需时间线进度。

![demo](docs/demo_zh.gif)

## 安装

需要：
- [Slay the Spire 2](https://store.steampowered.com/app/2868840/Slay_the_Spire_2/) (Steam)
- [.NET 9+ SDK](https://dotnet.microsoft.com/download)
- Python 3.9+

```bash
git clone https://github.com/wuhao21/sts2-cli.git
cd sts2-cli
./setup.sh      # 从 Steam 复制 DLL → IL patch + 无界面防护 → 编译（Debug + ReadyToRun）
```

或者直接运行 `python3 python/play.py`，首次会自动完成 setup。

已验证 **v0.111.0（Steam public-beta，构建 24724944）**。通过 Steam 更新游戏后，
请重新运行 `./setup.sh`，同步游戏 DLL、补丁和官方中英文文本并重新编译。
其他游戏版本可能需要修改适配代码。

可运行 `python3 python/play_full_run.py 5 Ironclad` 检查兼容性，并依次替换为
Silent、Defect、Regent、Necrobinder。“完成”指正常胜利或死亡；崩溃、卡住和超时
会返回非零退出码。

### 机器人按明确的策略做决定

任何客户端都不会替玩家做选择。机器人（`play_full_run.py`、`play.py --auto`、`agent/auto_play.py`、
`agent/combat_helper.py`、`scripts/fuzz_legal.py`、`scripts/bench.py`）通过 `python/policy.py`
里的策略从引擎的 `legal_actions` 中选一项，从不“默认选第一个”：`random`（`play_full_run.py --policy`
的默认值：按种子随机，按操作类型加权，同类型内均匀选择，从不丢弃药水）或 `heuristic`（`play.py --auto`
使用：只要有能打的牌就打，攻击生命最低的敌人；生命低于 70% 休息，否则升级；优先移除打击/防御；
领取全部奖励后继续；其余随机）。所有规则见 `python/policy.py` 的文档字符串。同一种子加同一策略可以完全复现。
奖励界面和宝箱永远是决策：客户端会处理 `rewards` / `treasure_relic`（仍会发送 `"manual_rewards": true`，引擎现在会忽略它）。

## 玩

```bash
python3 python/play.py                        # 中文交互模式
python3 python/play.py --lang en              # English
python3 python/play.py --ascension 10         # 渐进难度 10
python3 python/play.py --character Silent      # 选择静默猎手
```

不指定 `--character` 时会先询问要用哪个角色，每开新的一局都会再问（游戏的选人界面要玩家选定角色并确认后才会开局）；`--auto` 和非交互运行必须指定 `--character`。

游戏内输入 `help` 查看所有命令：

```
  help     — 帮助
  map      — 显示地图
  deck     — 查看牌组
  potions  — 查看药水
  relics   — 查看遗物
  piles    — 查看抽牌堆/弃牌堆/消耗堆（战斗中）
  quit     — 退出

  地图:    输入编号 (0, 1, 2)
  战斗:    输入卡牌编号 / e 结束回合 / p0 使用药水
  卡牌奖励: 输入卡牌编号 / s 跳过（奖励可跳过时）/ a0 其他选项
  奖励:    输入编号领取 / p 继续（未领取的奖励将被放弃）
  宝箱:    输入遗物编号 / s 跳过
  选牌:    输入卡牌编号 / s 跳过或取消（界面允许时）
  休息:    输入选项编号 / leave（游戏允许离开时）
  事件:    输入选项编号 / leave（事件没有可选项时）
  商店:    c0 买卡 / r0 买遗物 / p0 买药水 / rm 移除 / leave 离开
  药水:    任意提示下 u0 在战斗外使用（可用时）/ x0 丢弃
```

每个提示都根据引擎的 `legal_actions` 生成：只接受游戏在该界面允许的操作，不会替你做任何选择（出错后不会自动继续或离开，只会重新显示当前决策）。客户端没有专门界面的决策会显示全部字段和合法操作，编号为 `a0`、`a1`……

## 角色支持

| 角色 | 状态 |
|---|---|
| 铁甲战士 (Ironclad) | 完全可玩 |
| 静默猎手 (Silent) | 完全可玩 |
| 故障机器人 (Defect) | 完全可玩 |
| 亡灵契约师 (Necrobinder) | 完全可玩 |
| 储君 (Regent) | 完全可玩 |

## AI Agent 玩法：`sts2` 命令行客户端（推荐）

通过 Bash 工具玩游戏的 LLM agent 推荐使用 `bin/sts2`：每一步一条短命令，返回紧凑的文字视图和编号的合法操作，规则文字全部保留。
第一次调用会启动一个后台守护进程，持有一个引擎进程（unix socket 位于 `$TMPDIR`；引擎崩溃时会自动重启并重放本局；空闲 30 分钟后自动退出，可用 `STS2_IDLE_TIMEOUT` 调整），之后每次调用只需约 20 毫秒（其中 15 毫秒是 Python 启动；在 M 系列 Mac 上测得）。

```bash
bin/sts2 start --character Ironclad --seed abc --lang zh   # 必须指定 --character；[--ascension N] [--profile PATH]
bin/sts2 state                     # 当前决策 + 编号的可选操作
bin/sts2 do 3                      # 执行第 [3] 个操作
bin/sts2 do "play Bash E0; play Strike E0; end"   # 指令链：H#/E#/P# 和编号指你看到的视图；遇到错误、
                                   # 新房间/新回合/战斗结束或不适用的步骤时停止（见下文"do 指令链"）
bin/sts2 do "play Body Slam -> Jaw Worm"   # 支持多词牌名；完全相同的牌视为同一个操作，但同名而不同的牌
                                   # 或同名敌人需要用 H<n> / E<n>
bin/sts2 deck | relics | potions | piles [draw] | map | card N | log [N]
bin/sts2 --json state              # 输出引擎原始 JSON
bin/sts2 help                      # 全部指令（在本地输出，不会启动守护进程）
bin/sts2 stop [--forget]           # --forget 同时结束本局（否则下次调用会重放本局）
```

每次返回先列出上次决策以来发生的事（打出和抽到的牌、伤害、格挡、能力、敌人行动、遗物触发、金币/牌组/遗物变化），然后是当前决策：战斗中显示敌人的意图和能力、带卡牌说明的手牌、能量、充能球/辉星/奥斯提、牌堆；选牌时显示用途（升级/移除/变化/附魔/丢弃/消耗/选择）、游戏的提示语、来源、能否取消，以及升级/附魔/变化预览；还会显示先古之民的对话、每个目标的实际伤害（只有一个敌人时也显示）、游戏自己的意图名称、游戏结束时的分数和死因、商店删牌已使用，以及带坐标轴、图例和工具说明的水晶球格子。奖励界面会说明何时必须领取全部奖励（不能继续），并把 `proceed` 标为放弃未领取的奖励；卡牌奖励的“跳过”显示为跳过操作而不是其他选项（对它用 `alt N` 也是跳过）；不能打出的手牌会说明原因（`[不可打出：我没有足够的能量]`），药水显示为 `[目标：单个敌人，仅限战斗]`。选牌界面会说明要选几张、从哪里选（`选择 2 张牌 | 来自 涅奥 | 牌组中的牌`、`选择 0 或 2 张牌`）；X 辉星费用的牌显示为 `(0 X*)`；发光的手牌会注明（`[金色高亮：条件已满足]`）；可以无视路线时地图会用游戏的原文说明（这类走法标有`（无视路线）`）；被偷走后归还的卡牌奖励显示其牌名。任何视图都不会输出 `None`（商店之外的 `移除一张牌` 不带价格）。渲染器不认识的字段会以 `key: value` 输出。每次输出都小于 8 KB，且合法操作永远不会被截掉（长列表的选牌操作会合并成一行 `[a-b]`，改为缩短卡牌列表）。标题行显示幕、楼层、房间、Boss、**进阶和种子**（游戏结束界面也会显示）。每段事件只显示一次：`do` 之后紧接着的 `state` 不会重复 `do` 已经显示过的事件（`sts2 log` 可以再看）。伤害行会说明每个数字的含义：`蟾蜍蝌蚪 -> 铁甲战士 受到 12 点伤害（另有 12 点被格挡）`（失去 12 点生命，格挡吸收 12 点）、`…… 格挡了全部 6 点伤害`；格挡行为 `铁甲战士 获得 5 点格挡`。`sts2 map` 在上方标出列号（COL）、左侧标出行号（ROW，以及对应楼层 `f`），标出当前位置（`<x>`），列出合法的 `go COL ROW = 房间类型`，并在有可走节点的行尾注明；地图决策的操作可以照抄输入（`go 3 2 未知` -> `sts2 do "go 3 2"`）。`click X Y` 必须带工具（`big` = 3x3，`small` = 单格）。牌名会匹配手牌中所有同名（或以它开头）的牌，不论能否打出：手里有一张不能打出的防御和一张能打出的偏斜时，`play Def` 会报歧义并列出两张，绝不会替你打出另一张；指定的牌不能打出时会报错并说明原因。各命令会拒绝不接受的参数（`log x`、`map extra`：输出用法，退出码 1）。开局命令连同引擎实际使用的种子（即使你没有指定）保存在 `$TMPDIR`，所以引擎崩溃、空闲时退出或守护进程被杀后，重放的都是同一局；重放结果与原局面不一致时会明确提示。守护进程停止时（`sts2 stop`、空闲超时）会从 `$TMPDIR` 删除自己的 socket、锁文件和空日志，只保留本局记录文件，下次调用可以接着玩；`sts2 stop --forget` 连它一起删除。`--lang zh` 使用游戏官方中文。守护进程里永远不会开启调试命令。设置 `STS2_SESSION` 可以同时进行多局互不影响的游戏。

**`do` 指令链与编号。** 建议用牌名（`play 打击`、`play 痛击 蟾蜍蝌蚪`）：每当有牌离开手牌或敌人死亡，H#/E# 就会重新编号，`play H0` 之后原来的 H1 就变成了 H0。为了让编号步骤也安全，指令链里的每个编号都指**提交指令链时显示的视图**（上一次 `state`/`do` 的输出）：

- `H#` / `P#`：就是那张牌 / 那瓶药水，按实例编号（`uid`）找到它现在的位置（`play H0; play H1` 打出的是显示为 H0 和 H1 的两张牌；位置变了时输出会注明 `(shown as H1)`）。如果它已经离开（被打出、丢弃、消耗、变化），指令链停止——`play H0; play H0` 绝不会再打出另一张牌。
- `E#`：就是那个敌人，按 `combat_id` 查找；已死亡或不可选中则停止。
- 操作编号 `N`——所有编号都一样（`select`、`skip`、`proceed`、`leave`、`remove`、`end`、药水、`claim`、`buy` 等）：指那个视图里打印的列表中编号为 N 的操作，按同样方式重新对应，而且只在显示时的那一个界面上执行（见下；战斗中是同一回合，奖励 / 商店界面是它剩下的项目）。在商店的移除界面上执行 `skip; remove; 13` 会在 `13` 之前停止：`remove` 重新打开了移除界面，而这个新界面从没显示给你。
- 界面编号（`choose N`、`pick N`、`claim N`、`buy card N`、`select i,j`、`alt N`、`go N`）：只在显示时的那一个界面上执行——决策、房间、来源、用途、提示语、所列项目（按 `uid`）以及引擎的 `selection_id` 都相同（已领取的奖励 / 已买下的商品可以不在了）——且该编号对应的项目没变。如果是指令链没有给你看过的界面，会在这样的步骤之前停止：`claim 2` 打开的卡牌奖励、`remove` 打开的选牌界面、事件的下一页，或者同类型的另一个选牌界面（在燃烧契约的消耗界面上执行 `select 2; play 坚毅+; select 0`，会在 `select 0` 之前停止：坚毅+ 的界面从没显示过）。停止时会说明哪里不同：`它来自 坚毅+（显示的那个来自 燃烧契约）`、`移除界面已关闭并重新打开（新的选择）——它没有显示给你`、`事件已进入另一页`、卡牌 / 选项不同、出现了没有显示过的奖励。`go COL ROW` 指的是地图节点，不算编号。
- 不带编号的步骤（`end`、`skip`、`proceed`、`leave`、`remove`、`select none`、`go COL ROW`、牌名和敌人名）按游戏当前所处的状态解读：`claim 2; skip` 会跳过 `claim 2` 打开的卡牌奖励。奖励界面上的 `skip` 只有作为指令链的第一步时才等于 `proceed`；指令链后面的 `skip` 绝不会离开奖励界面（那会放弃未领取的奖励）：`claim 0; skip` 和 `pick 0; skip` 会在 `skip` 之前停止，并说明会放弃什么。确实要离开请写 `proceed`。
- 不在所显示列表中的操作编号是错误（不是停止），并列出游戏当前的合法操作。
- `raw` / 引擎动词（`play_card card_index=2`）按原样发送。

每一步之后，只要下一步在游戏当前所处的决策上合法且没有歧义，指令链就继续执行，即使决策类型变了（`pick 1; proceed`：卡牌奖励 -> 奖励界面；回合中途选牌之后的 `select 1; end`）。以下情况会在下一步之前停止：进入了新房间、战斗开始或结束、新回合开始或本局结束；该步骤不适用于新的决策（弹出选牌时的 `play`：“仍在战斗中，但选牌界面（由头槌打开）正在打开：请先完成选择”）；`select` 步骤要回答的选择根本没有打开，因为游戏自己选好了牌（弃牌堆只有一张牌时的 `play 头槌; select 0`：“游戏已经替这次选择选好了牌：头槌：自动选择了 打击（只有 1 张可选）”）；药水步骤将在指令链开始时那一个界面以外的任何界面上执行（战斗中是同一回合；之后打开的界面不算，即使看起来一样：在商店的移除界面上执行 `skip; remove; discard P0` 会在 `discard P0` 之前停止）；编号无法安全对应（见上）；以及遇到第一个错误。

奖励编号是**固定的**：领取奖励 0 不会让其他奖励重新编号（`claim 0; claim 1` 领取的是显示为 0 和 1 的奖励），这一点与手牌/敌人编号不同。领取已经不在的奖励时会提示 `奖励 0（8 金币）已经领取过了`。步骤出错时只给出一段用输出语言写的说明：该编号现在指什么、游戏为什么拒绝（只说一次；引擎的标志放在括号里）以及合法操作，例如 `H2 现在是 防御（Defend，技能，1 能量，目标：自己）；它不需要目标：play H2，不要加 E#。现在可选：[1] ……`、`H1 现在是 打击（……）；敌人不止一个，需要指定目标：E0 蟾蜍蝌蚪, E1 海洋混混（例如 play H1 E0）`，或 `H0 现在是 坚毅+（技能，1 能量，目标：自己），不能打出：我没有足够的能量 (EnergyCostTooHigh)`（E#、P#、select、claim、buy、pick、choose 和 go 也一样）。引擎的每一条错误消息（tests/test_sts2_messages.py 从 C# 源码中读出全部消息）都有这样的中英文说明，匹配不区分大小写，而且不会重复编号说明里已经说过的内容：`select 0,0` -> `卡牌 0 是 打击；卡牌 0 列了两次：每张牌只能选一次`，在要选 2-3 张牌的界面上 `select 0` -> `此界面需要选择 2-3 张牌，而不是 1 张`，`go 9 9` -> `(9,9) 在地图之外（列 0-6，行 0-16；sts2 map）`（合法走法只在列表里出现一次），在只有跳过按钮的卡牌奖励上 `alt 1` -> `这个卡牌奖励没有其他选项（除选牌外只有跳过按钮）：请用 'skip'（操作 [4]）或选择一张牌`。`--json` 保留引擎的原始消息。说明里用的是你输入的编号：如果指令链前面的步骤让它换了位置，会注明——`H2（指令链开始时显示的 防御，现在是 H1）是 防御（……）`。在没有对应界面的决策上执行界面步骤时，会（只说一次）说明当前处于什么界面并列出合法操作（`'go 0' 用于选择地图选项，但现在处于战斗回合：这里没有这类选项。现在可选：[1] ……`）；任何提示都不会出现空的编号范围。使用 `--lang zh` 时，操作标签（`[3] 打出 H1 痛击 -> E0 蟾蜍蝌蚪`、`丢弃药水 P0 火焰药水`）、决策名称（选牌界面、地图、事件页面）和提示都是中文；输入的步骤仍按 `sts2 help` 中的写法（`play`、`select 0,2`、`go 3 2`），作为提示给出的命令也按原样显示（`sts2 do "select 0,2"`）。

`agent/sts2_bridge.py`（HTTP，每次 POST 一条 JSON 命令）仍然可用：现在引擎退出后会自动重启，并用引擎报告的种子重放本局（重启后的响应带 `notes`，重放不一致时会说明），非法请求体会返回 JSON 错误，提供 `GET /state` 和 `GET /help`，`--compact` 模式保留所有说明文字、附魔、苦难以及升级/附魔预览，只删除：`id` / `text_key` / `act_name` 字段、null 值和空列表/空对象、完整的 `deck`（用 `get_deck` 获取），以及说明里已经填好数值时的 `vars`；牌堆列表变成 `"牌名 xN"` 字符串，卡牌的附魔/苦难并入牌名（`"Strike [Sharp 2]"`）。附魔界面顶层的 `enchantment` 仍是对象 `{name, amount, description}`。`agent/combat_helper.py` 只打合法的牌，遇到任何非战斗决策都会停下交还给调用者；评分相同的牌、目标或药水由带种子的随机数决定，而不是按列表顺序。

## JSON 协议

除了交互模式，也可以通过 stdin/stdout JSON 协议编程控制（写 AI agent、RL 训练等）：

```bash
dotnet run --project src/Sts2Headless/Sts2Headless.csproj
```

```json
{"cmd": "start_run", "character": "Ironclad", "seed": "test", "ascension": 0}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "action", "action": "end_turn"}
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}
{"cmd": "action", "action": "skip_card_reward"}
{"cmd": "quit"}
```

每个命令返回一个 JSON decision point（`map_select` / `combat_play` / `card_reward` / `card_select` / `bundle_select` / `rest_site` / `event_choice` / `shop` / `crystal_sphere` / `rewards` / `treasure_relic` / `game_over`），所有名称为英文。

- **开局：** 必须提供 `character`（`Ironclad`、`Silent`、`Defect`、`Regent` 或 `Necrobinder`）：游戏的选人界面打开时会高亮第一个角色，但只有玩家选定角色并确认出发后才会开局，而协议不会替玩家确认，所以缺少或为空时返回 `bad_request`，未知角色返回 `unknown_character`。`seed` 缺省、为 null、为空或只有空白时使用随机种子，和游戏里留空的种子框（“随机”）一样；实际使用的种子见 `context.seed`。`ascension` 默认为 0（游戏界面会记住上次的进阶等级；这里不填就是 0），`lang` 默认为 `en`（只能是 `en` 或 `zh`，其他值返回 `bad_request`）。每个字段都会检查类型：类型错误返回 `bad_request` 并指出字段名（`modifiers` 是字符串列表，`manual_rewards` / `omit_deck` 是布尔值，`profile` / `game_mode` / `seed` 是字符串）；`manual_rewards` 仍然接受，但会被忽略。
- **一个进程跑多局：** 再发一次 `start_run`（或 `load_save`）就会结束上一局、开始新的一局，省掉引擎启动。批量跑或 RL 建议每个 worker 只开一个进程，跑上千局内存也不会增长。`python/engine.py` 里有一个简单的 `Engine` 封装：直接用 `dotnet` 运行编译好的 dll，读取有超时，stderr 会被读走。
- **启动：** 约 40 毫秒后返回 `{"type":"ready","version":...,"localization":true}`，随后立即进行一次性的引擎初始化（约 0.15 秒，另外在第二个线程上预热游戏的存档序列化器）。客户端在第一次 `start_run` 前稍有停顿就不必再等它；在这之前发来的命令会排队等待。设置 `STS2_NO_WARMUP=1` 则和以前一样推迟到第一次 `start_run`/`load_save`。找不到 `localization_eng/` 文本时 `localization` 为 false 并附带 `warning`，此时名称都是原始键名；查找顺序是 `STS2_DATA_DIR`、从程序所在目录向上、`lib/` 旁边（`STS2_LIB`、引擎实际加载的 lib 目录，或启动器指向仓库 `lib/` 的 `STS2_GAME_DIR`）、从当前工作目录向上；warning 会列出所有查找过的目录。
- **编译产物：** `setup.sh` 还会把 ReadyToRun（预编译）版本发布到 `src/Sts2Headless/bin/R2R/net9.0`，启动更快、每步约快 30%。`python/engine.py` 优先使用 `$STS2_ENGINE_DLL`，否则在 `bin/R2R`、`bin/Release`、`bin/Debug` 中选最近编译的那个（比 `lib/sts2.dll` 旧的 R2R 版本会被跳过），所以改完代码直接 `dotnet build` 就会生效。
- **空字段：** 响应里嵌套的对象（卡牌、遗物、药水、能力等）不再输出值为 null 的字段，例如没有关键词的卡牌没有 `keywords` 键；响应本身的顶层字段始终存在。可选字段请用 `.get(...)` 读取。
- **合法操作：** 每个决策都带一个 `legal_actions` 列表，列出可以直接发送的操作，例如 `{"action":"play_card","args":{"card_index":0,"target_index":1}}`。需要选多张牌时只有一条 `{"action":"select_cards","choose":{"from":[0,1,2],"min":1,"max":2}}`，发送时用 `{"indices":"0,2"}`（或 `[0,2]`）。Crystal Sphere 小游戏也只有一条 `{"action":"crystal_sphere_click","choose_cell":{"cells":[[x,y],...],"tools":["big","small"]}}`，发送时用 `{"x":..,"y":..,"tool":..}`。总的规则是：条目里没有 `args`、而是带 `choose*` 键时，需要客户端自己填参数。`usage` 为 `Automatic` 的药水（例如 Fairy in a Bottle）不会出现在 `use_potion` 里，直接发送也会被拒绝。非法输入会直接报错，不再被悄悄忽略，比如走不到的地图节点、锁定或越界的选项、错误的卡牌序号或张数。`scripts/fuzz_legal.py` 会随机执行合法操作，只要收到报错就算失败。
- **严格校验操作：** 当前决策不允许的操作会被拒绝，返回 `{"type":"error","code":"illegal_action","message":"'leave_room' is not allowed now: ... Current decision: event_choice. Legal: choose_option(option_index=0|1|2)","decision":"event_choice","legal_actions":[...]}`。校验是精确的：操作名**和**参数都必须与 `legal_actions` 中的某一条一致（不能打出的牌、越界或已死亡的目标、给不针对单个敌人的牌传 `target_index`、买不起的商品都会返回 `illegal_action`，原因写在 message 里）；引擎不会替你改目标或做选择。引擎唯一会补上的参数是只有一条匹配时的 `target_index`（只有一个可攻击的敌人）。带 `choose` / `choose_cell` 的条目由各自的处理函数校验（序号、张数、格子）。同一个按钮的别名：事件结果页上 `leave_room` / `proceed` 等于“继续”选项；`rewards` 上 `leave_room` / `skip_rewards` 等于 `proceed`；`treasure_relic` 上 `proceed` / `leave_room` 等于 `skip_treasure_relic`；休息处和商店里 `proceed` 等于 `leave_room`。`leave_room` 不能跳过事件或休息处；只有 `cancelable` 为 true（界面上有取消/跳过按钮）的选牌才能 `skip_select`；`min_select` 为 0 且没有这种按钮的界面，用 `indices` 为 `""` 或 `[]` 的 `select_cards` 不选牌直接确认。所有操作错误都带有 `decision` 和 `legal_actions`；未知操作会提示"did you mean"；`args` 里的序号必须是整数（数字字符串也可以），`target_index` 必须指向可以选为目标的敌人（也可以发送它的 `target_id`）。药水与游戏的“使用”按钮一致：仅限战斗的药水只能在自己的战斗回合、且没有打开选牌界面时使用（其他时候会被拒绝，而不是被白白丢掉）；随时可用的药水（鲜血药水、果汁等）任何时候都能用，`legal_actions` 在每个决策都会列出。格式错误的输入（不是 JSON、不是对象、缺少 `cmd`、`ascension` 不在 0-10 等）返回 `"code":"bad_request"`。操作不接受的参数（例如 `end_turn` 带 `{"x":1}`）返回 `illegal_action`，不会被悄悄忽略（各操作的参数见 `help`）；值为 `null` 的参数视为没有提供；索引参数不是整数时返回 `bad_request`。除非设置 `STS2_DEBUG=1`，错误里不再带 .NET 堆栈。如果某个操作触发的游戏流程在游戏内部失败，会返回 `"code":"flow_failed"`，`state` 里是之后的决策。
- **选牌：** `card_select` 会像游戏界面一样说明用途：`purpose`（`upgrade` 升级 / `remove` 移除 / `transform` 变化 / `enchant` 附魔 / `discard` 丢弃 / `exhaust` 消耗 / `choose` 选择）、`prompt`（游戏的提示文字，例如"选择1张牌来消耗。"）、`cancelable`（界面上有取消/跳过按钮，例如休息处锻造、商店移除卡牌；此时 `skip_select` 合法，用于退出）、`screen`（`hand` / `deck` / `grid` / `draw` / `discard` / `exhaust` / `choose_a_card` / `reward_grid`）和 `source`（发起选择的卡牌、遗物、药水、能力或事件 `{name, kind}`）。附魔界面另有 `enchantment {id, name, amount, description}` 和每张牌的 `after_enchant`；结果固定的变化有 `transforms_to`。战斗中 `combat` 字段给出战场（能量、手牌、带意图的敌人、能力、充能球、牌堆张数），相当于游戏里的"查看战场"按钮。凡是游戏会弹出界面的选牌都是一个决策，即使只有一张牌（引擎从不替你选）：只有在游戏自己跳过界面时才会跳过（可选牌数不超过必选张数且不需要手动确认、手牌里只有一张可升级的牌等）。三选一界面（发现、丰饶、药水）的 `min_select` 为 1，只有游戏在该界面显示"跳过"按钮时才有 `skip_select`（此时 `cancelable` 为 true）。`min_select` / `max_select` 是游戏界面实际能确认的选牌张数，按界面类型而定：牌组附魔界面（木札、扭曲锤子）和牌组升级界面不选牌就无法完成（预览里的确认按钮在没有选牌时不起作用），所以木札的"至多 3 张"是 `min_select` 1；牌组变化/移除界面、手牌界面（净化、赌徒特酿、灰水等）、战斗牌堆界面（涅奥之怒）和网格界面（海玻璃）与各自确认按钮的规则一致。`min_select` 为 0 的界面（利爪、净化、赌徒特酿、涅奥之怒、海玻璃等）和游戏的确认按钮一致，不选牌也能确认：`legal_actions` 给出 `min` 为 0 的 `choose`（只能选一张的界面还会列出 `indices` 为 `""` 的 `select_cards`），`indices` 为 `""` / `[]` 的 `select_cards` 表示不选任何牌直接确认；除非界面有取消按钮（`cancelable`，只有牌组界面有），否则没有 `skip_select`。可选张数不连续的界面（选满 `max_select` 张自动完成、也可以不选直接确认的网格）另有 `select_counts` 和 `choose.counts`。游戏不截断的地方，引擎也从不把要求的张数截断为可选的牌数：用现有的牌无法确认的界面（锻造要求 2 张而只有 1 张可升级的牌、需要手动确认且牌数少于 `min_select` 的牌组界面、只有选满 `max_select` 张才自动完成的网格）保留原本要求的 `min_select` / `max_select`，并带 `select_counts: []`；如果它是 `cancelable`，唯一合法的选牌操作是 `skip_select`（界面的关闭按钮），`message` 会说明；否则就是软锁（见“软锁”）。游戏自己的截断保持不变：可选牌数不超过 `min_select` 且不需要手动确认时根本不弹出界面；战斗牌堆界面（涅奥之怒）只按显示出来的牌计数。调试：在休息处用 `set_player {"smith_count": 2}` 设置锻造张数；`set_player {"card_select": {"method": "upgrade"|"transform"|"deck"|"grid", "min": 1, "max": 3, "cancelable": false, "manual": false}}` 通过游戏的 CardSelectCmd 按这些参数打开一个牌组选牌界面。升级界面里每张牌带 `after_upgrade`；它和游戏的升级预览一样升级这张牌本身的副本，所以这张牌保存的状态（打出过的遗传算法增长后的格挡等）、本回合或本场战斗内改过的费用（开悟、异蛇之油、癫狂之触等）以及战斗中卡牌受到的战斗费用和数值修正都会体现出来。
- **事件日志：** 每个决策带 `events`，按顺序列出上次决策以来发生的事：`turn` 回合标记、`card_played`、`draw`、`damage`（`source`、`target`、`amount`、`blocked`、`killed`）、`block`、`power`、`enemy_move`（带当时显示的意图）、`card_added`（带 `pile`：`hand` / `draw` / `discard` / `exhaust`，即生成的牌去了哪里，和游戏里卡牌飞向牌堆的动画一致；本应进入手牌但手牌已满而进了弃牌堆时带 `"redirected": "hand_full"`；没有进入任何牌堆的生成牌，例如结束战斗的那一击打出的愤怒的复制品，游戏不显示，也没有这一条）、`discard`、`exhaust`、`orb_channeled`、`potion_used`、`potion_queued`（在卡牌的选择还没结束时使用的药水会排队，等这张牌结算完再生效，和游戏一样）、`relic`（遗物触发）、`enemy_died`、`speech` / `thought`（游戏的对话气泡和思考气泡：`speaker`、当前语言的 `text` 和本地化 `key`，例如手牌已满时角色的“我的牌太多了……”、没有牌可抽时的提示，怪物的台词，如知识恶魔的“做出选择。”，以及游戏自动打出的牌被阻止时的“我被凡庸所阻挡。”这类台词），`card_auto_selected`（游戏没有打开选牌界面、自己完成了选择，因为它能拿走所提供的全部卡牌：弃牌堆只有一张牌时的头槌、只有一张可升级牌时的武装、可选牌数不超过最少选择数的任何选择；带 `source`、`purpose`、`screen`、`cards`、`available`、`min_select`、`prompt`），以及整局层面的变化 `gold`、`hp`、`max_hp`、`deck_added` / `deck_removed` / `deck_changed`、`relic_obtained`、`potion_obtained` / `potion_lost`。`gold` 每次变化一条，顺序和游戏结算顺序一致：商店购买是 `{"change": -73, "reason": "spent", "item": "交锋"}`，遗物给的金币是 `{"change": 15, "source": "招财异鱼"}`（商人先把牌加入牌组，所以招财异鱼的 +15 在扣款之前；扣的价格就是商店显示的 `cost`，已包含会员卡、送货员等折扣）；`reason` 为 `spent` / `lost` / `stolen`。能力的名字和游戏里的标题一致：临时能力用施加它的卡牌、药水或遗物的标题（速度药水、预判、热修复）。生物的名字是事件发生那一刻敌人列表里的名字（结实的卵孵化后，之后的伤害和死亡记录都写幼虫）。最多 80 条。
- **查询（不推进游戏）：** `{"cmd":"get_state"}` 重新发送当前决策；`get_deck`（`"upgrades":true` 附带升级预览）、`get_relics`、`get_potions`、`get_piles`（`"pile":"draw"|"discard"|"exhaust"|"hand"`；抽牌堆已排序，不暴露顺序）、`get_card`（`"deck_index":N` / `"hand_index":N` / `"id":"BASH"`，`"upgraded":true`）、`get_map`、`help`。
- **精简决策：** 在 `start_run` / `load_save` 里加 `"omit_deck": true`，每个决策都不再附带 `player.deck`（保留 `player.deck_size` 和随牌组变化而变化的 `player.deck_version`；需要时用 `get_deck`）。默认行为不变。返回的 JSON 把 `type`、`decision`、`legal_actions` 放在最前面，被截断时也能看到。商店物品另有 `price`（金币），卡牌另有 `energy_cost`，因为商店里的 `cost` 指金币，其他地方指能量。
- **调试命令：** `set_player`、`enter_room`、`set_draw_order` 会修改或暴露玩家无法得知的状态，因此除非引擎以 `STS2_DEBUG=1`、`STS2_DEBUG_COMMANDS=1` 或 `--debug` 启动，否则返回 `"code":"debug_disabled"`。`set_player` 的牌组 id 末尾加 `+`（`"BASH+"`）表示加入升级后的牌。`enter_room` 可以带 `"act": 1-3`，先进入那一幕（RunManager.EnterAct），例如去打最后一幕的 Boss。
- **文字：** 所有 `description`（卡牌、遗物、药水、能力、事件）都是游戏自己的文字，数字已经填好，和游戏界面上看到的一样。卡牌数值会随战斗中的力量、虚弱等实时变化。图标会转成文字（`1点能量`、`★`）；跟在数字后面当单位用的能量图标只写成"点能量"（`耗能变为0点能量`、`获得4点能量`）。手牌、奖励、选牌、商店里的卡牌，以及药水和奖励中的遗物，还带有 `tips`：也就是游戏在它们旁边显示的二级提示，比如"易伤""消耗"等关键词和能力的解释；能力（玩家、敌人）也带 `tips`（`PowerModel.ExtraHoverTips`，例如覆甲的格挡、活力火花的污染）。`tips` 和游戏的提示集合完全一致（`IHoverTip.RemoveDupes`）：两条提示可以同名（带污染的牌同时显示“污染”苦痛和它施加的“污染”能力），游戏不显示的提示也不会添加（游戏没给提示的金色词保持原样；狂乱逃离的“远离。”只是普通文字，它的提示是“沙坑”）。能力文字是悬停提示里带拥有者信息的智能描述：按拥有者区分措辞的条目在敌人身上写“其回合”（仪式、中毒、灾厄、缓慢、活力）；不区分的条目（覆甲）在敌人身上也写“你的回合”，和游戏一样——这个能力列在拥有它的敌人下面。
- **牌堆内容：** `combat_play` 里有 `draw_pile` / `discard_pile` / `exhaust_pile`，每张牌都带 `description` 和 `keywords`，和游戏里的牌堆界面一样（文字按牌堆界面的卡牌渲染：计入附魔和计算值，例如编译冲击的“（抽1张牌）”，但不计入力量等只在手牌中生效的修正）。抽牌堆按 `{name, count, ...}` 分组，并像游戏一样按（稀有度, id）排序，不暴露抽牌顺序。弃牌堆和消耗堆每张牌一条（`count` 为 1），按真实顺序排列，和游戏显示的一样。
- **卡牌：** 所有卡牌条目（手牌、牌堆、牌组、奖励、card_select、卡包、商店）的名字和游戏标题一致（`打击+`；可以多次升级的牌为 `名字+N`），并带有 `upgraded`、`upgrade_level`（已升级或可多次升级时）、`max_upgrade_level`（可多次升级的牌）和 `rarity`。X 费牌有 `costs_x: true`（`cost` 仍为 0）。有星辰费用的牌（储君）有 `star_cost`（0 或以上），X 星辰费用的牌有 `star_cost_x: true`。永远无法打出的牌有 `unplayable: true`。所有列表（牌组、`get_deck`、`get_card`、各种用途的 card_select、卡牌奖励、卡包、商店、特殊卡牌奖励、卡牌预览）都按游戏卡面显示卡牌：`description` 和 `stats` 包含附魔带来的数值（附有锋利 2 的完美打击显示“造成8点伤害”；菲涅耳透镜的灵巧会加格挡），并带 `enchantment` / `enchantment_amount` 和 `affliction` / `affliction_amount`。附魔牌的 `after_upgrade` 也保留附魔，附魔界面的 `after_enchant` 也会预览新附魔自身的数值。手牌里附魔追加的那行也跟随实时修正（有 2 点敏捷时，伶俐的“获得3点格挡”显示为 5）， `star_cost` 已包含修正（虚空形态、艳丽围巾），`x_value` 是此刻打出时 X 的值（能量；X 星辰牌则为星辰数，并经过游戏的 X 修正：化学物X 加 2；`damage_by_target` 用同一个 X），`can_play` 就是游戏自己的判断。为 false 时，`unplayable_reasons` 列出原因（`EnergyCostTooHigh`、`StarCostTooHigh`、`HasUnplayableKeyword`、`BlockedByHook`、`BlockedByCardLogic`、`NoLivingAllies`），`blocked_by` 给出阻止者的名字（例如凡庸、烟雾弥漫），`unplayable_message` 是尝试打出时角色说的那句话（“我没有足够的能量。”“我被烟雾弥漫所阻挡。”“我不能打出这张牌。”；被拒绝的 `play_card` 也会带上它），`cost_crossed: true` 表示游戏在费用上画了划掉的图标（原因不是能量或星辰时）。游戏高亮的牌带 `glow`（`gold`/`red`）。`legal_actions` 只看 `can_play`。商店里的卡牌、遗物和药水也带 `id`。
- **手牌顺序：** `hand`（以及 `play_card` / `get_card hand_index` 使用的 `index`、`get_piles hand`、从手牌选牌时 `card_select` 的 `cards`）按游戏手牌从左到右显示的顺序排列。它不一定是手牌堆的顺序：在手牌选择中选中的牌（武装的升级、癫狂之触、双持等）在选择期间离开手牌行，等发起选择的卡牌或药水结算完后被放回最右边（NPlayerHand.OnSelectModeSourceFinished）；进入手牌的牌加在最右边；在手牌中被变化的牌保持原位。只有一张牌符合条件、游戏不打开选择界面时（例如只有一张可升级的牌时打出武装），顺序不变。游戏自身的逻辑（随机选牌等）仍然使用手牌堆的顺序。
- **敌人与目标：** `enemies` 列出可以选为目标的敌人，`index` 就是要发送的 `target_index`。每个敌人还有 `monster_id`，以及整场战斗不变的 `combat_id`：在 `play_card` / `use_potion` 里可以用 `target_id` 代替 `target_index`，这样回合中途有敌人死亡也不会打错目标。手牌和药水带 `uid`：实例编号，那张牌留在手牌中（手牌变化时它的 `index` 会变）、那瓶药水留在原栏位期间保持不变（不是操作参数；`bin/sts2` 用它在 `do` 指令链里对应 H#/P#）。`card_select` / `card_reward` 里的卡牌也带同样的 `uid`，`card_select` / `card_reward` / `bundle_select` 还带 `selection_id`：游戏每打开一个选择界面就有一个编号，所以卡牌相同的两个界面（燃烧契约的消耗界面、之后坚毅+ 的消耗界面）也能区分。仍在战斗中但不能被选为目标的敌人（例如等待重新接上的残杀千足虫节段）放在 `inactive_enemies` 里，带 `reviving: true` 和它接下来的行动。意图带 `title` 和 `description`（游戏里的意图提示，例如"这个敌人将会给你2张状态牌。"），状态牌意图还带 `card_count`。能力（玩家、敌人、奥斯提）带 `id`、`name`、`description`、`amount`、`type`（`Buff`/`Debuff`）和 `tips`。对帝皇蟹时，`player_facing` 为 `Left`/`Right`，每只蟹有 `behind_player`（它的攻击伤害 x1.5；以它为目标会让你转身）。`damage_by_target` 每行带 `target_id`，`repeat`/`total_damage` 会用 CalculatedHits（撕裂、终结技等）；牌会被打出不止一次时（士兵炖汤或附魔的重放、回响形态、爆发、复制、投斧、双打组合，即游戏的打出次数）还带 `plays`，此时 `total_damage` = `damage` x `repeat` x `plays`，手牌也带 `play_count`，描述里有游戏的“重放N。”一行；也覆盖奥斯提的攻击（`dealer: "osty"`）和随机目标的牌。`osty` 带 `powers`。故障机器人只要有充能球栏位就一定有 `orbs` / `orb_slots`（没有充能球时 `orbs` 是空列表）；每个充能球带 `description` 和 `evokes_next`（index 0 是最右边、最先被激发的充能球）。
- **自定模式：** `start_run` 支持 `"modifiers": ["SEALED_DECK", "MIDAS", ...]`（名字写错时会返回所有可用的修改器；Character Cards 要写明角色，例如 `"CHARACTER_CARDS:DEFECT"`）和 `"game_mode": "standard"|"custom"|"daily"`（传了修改器时默认为 `custom`）。会重建牌组的修改器（Draft、Sealed Deck、Insanity）会像游戏里一样在 Neow 加上对应选项。每日挑战的种子和修改器是游戏界面按服务器日期生成的，这里不复现；可以用 `"game_mode":"daily"` 把它们显式传进来。
- **奖励总是由你决定：** 和游戏的奖励界面一样，不会替你领取任何东西。战斗结束后会返回 `rewards` 决策，列出每个奖励（`gold` / `relic` / `potion` / `card` / `special_card` / `card_removal`，`index` 固定不变）。`claim_reward {"reward_index": i}` 领取其中一个，领卡牌奖励会弹出平常的 `card_reward` 选择；`proceed` 放弃剩下的。和游戏的奖励界面一样，全部领完后界面仍然保留：`rewards: []`，只有 `proceed`（这时仍可使用或丢弃药水），直到你选择继续。每个房间都有自己的奖励，用调试命令 `enter_room` 进入的房间也是如此。宝箱打开时先给金币，然后返回 `treasure_relic` 决策，用 `select_treasure_relic {"relic_index": i}` 选择，或用 `skip_treasure_relic` 跳过。事件和遗物发放的奖励（Neow 的选项、大锅、召唤铃铛、失物盒等）也会以 `rewards` 决策出现，并带有 `"source":"event"`；游戏不允许跳过时（涅奥骨骰）决策里 `"can_skip": false`，领完所有奖励之前不会提供 `proceed`。`start_run` / `load_save` 仍接受旧的 `"manual_rewards"` 参数，但会忽略它（自动领取模式已删除）。
- **档案：** 在 `start_run`（以及 `load_save`；`bin/sts2 start --profile PATH`）里加 `"profile": "<progress.save 路径>"`，就按这个档案的解锁和进度来玩，和游戏一致：解锁的内容、从未发现过的替代章节会被强制选中、每章第一个没见过的 Boss 和第一局固定的密林房间顺序（发现顺序），只有揭示了 Neow 纪元才会有 Neow，以及档案里的先古访问次数、建筑师胜场、旺购积分。只读取文件，不会写入。不提供档案时全部内容都已解锁、已发现（不做发现顺序调整）。
- **真实游戏规则（TestMode）：** 引擎以游戏的 TestMode 运行，但所有会改变玩法的 TestMode 分支都走真实游戏的逻辑：商店药水价格带有游戏用商店随机数生成的 ±5% 浮动（之后商店的随机结果也因此保持同步），大锅和召唤铃铛给出随机的药水/遗物（而不是测试用的固定列表），章节选择和发现顺序房间按档案决定，奖励组从不自动领取，滚石从左到右逐个击中每个敌人，跑图记录（因此也包括 `write_continue_save`）会记下每个地图节点的金币/生命值和商店里没买的商品，选择卡包界面保留游戏的提前退出判断。每个补丁都会检查是否找到了对应的 TestMode 分支；如果游戏代码变了，`start_run`/`load_save` 会返回错误，而不是悄悄按测试规则运行。设置 `STS2_TESTMODE_AUDIT=1` 会列出游戏代码里尚未在 `src/Sts2Headless/testmode-audited.txt` 中归类的 TestMode 判断（`tests/test_testmode_parity.py` 会运行它）。调试：`set_player {"obtain_relic": "CAULDRON"}` 获得遗物并触发拾取效果。
- **Crystal Sphere：** 这个事件的小游戏是 `crystal_sphere` 决策，包含 `rows`（`?` 未揭开、`.` 空、字母表示物品）、`hidden_cells`、`items_seen` 和 `divinations_left`。用 `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` 揭开格子，big 是 3x3 范围。不传 `tool` 时使用小游戏当前选中的工具（`current_tool`：游戏开始时是 big，之后保持上一次用的工具）。`items_seen` 的每一项有 `id`、`type`、`revealed_cells` 和 `fully_revealed`；物品完全揭开后才给出位置和大小（`x`/`y`/`width`/`height`），因为游戏里迷雾会挡住物品的其余部分。
- **事件：** `event_choice` 带有 `event_id`、正文 `description`（游戏显示正文时才有），古神事件（Neow 等）在第一次选择前还有开场对话 `dialogue`（`[{speaker: "ancient"|"character", text, key, next}]`）。游戏是在对当前档案和角色有效的对话里随机抽一段（`Rng.Chaotic`，不带种子，只影响表现；`AncientDialogueSet.GetValidDialogues`：第一次见面、该角色对应访问次数的对话，否则是可重复的对话池），所以无法预测具体是哪一句：`dialogue` 是按种子抽的一段，`dialogue_pool` 列出所有候选（`[{id, lines}]`，例如 `NEOW.talk.ANY.2-0r`），候选不止一段时 `dialogue_random` 为 true。每个选项的文字里名字都已填好（游戏用 StringVar 放的遗物、卡牌、附魔名；这时 `vars` 里对应的值是字符串），并带有 `tips`（悬停提示；卡牌预览带 `card`，即卡牌 id）、`relic`（古神选项给的遗物：`id`、`rarity` 等）、`will_kill`（游戏会让选项闪红：选了会死）和 `is_proceed`。选项结束事件后，返回事件的最后一页：`"finished": true`，结果文字在 `description` 里，只有一个"继续"选项；再发 `choose_option 0`（或它的别名 `leave_room` / `proceed`）进入地图；Neow 的最后一页也是这样显示。事件没有结束时，`leave_room` 会被拒绝。引擎不会替你离开事件。
- **软锁：** 游戏里没有任何可点的东西时（事件的所有选项都被锁定、未结束的事件没有选项、休息处在用过选项之前所有选项都不可用、用现有的牌无法确认且不可取消的选牌界面），决策会以 `"softlocked": true`、`softlock_reason` 和 `message` 返回，`legal_actions` 里只有游戏顶栏药水栏在这里仍然允许的操作：`can_use` 为 true 的随时可用药水的 `use_potion`，以及 `discard_potion`（事件锁住药水时，例如永恒之石，则什么都没有）；地图和牌组只能查看（`get_map`、`get_deck`）；游戏在这里同样会卡住，所以客户端（play_full_run、fuzz_legal、policy.py、`sts2`）把它当作本局结束。唯一的出路是 `{"cmd":"force_leave_room"}`，它**不是游戏行为**，不会出现在 `legal_actions` 里，只有设置 `STS2_SOFTLOCK_ESCAPE=1` 且当前决策是软锁时才能用（回到地图；软锁的 `card_select` 则是不选任何牌关闭界面）。
- **审判：** 坚持到底会打开游戏的放弃本局确认弹窗（NAbandonRunConfirmPopup）：返回 `event_choice`，带 `popup`（`id: "abandon_run_confirm"`，`header`、`body` 来自 main_menu_ui）和它的两个按钮：`0` 好的（`will_kill`）、`1` 不了。选“好的”会放弃本局（RunManager.Abandon）：`game_over`，`victory: false`、`abandoned: true`；选“不了”关闭弹窗，回到同一页。
- **建筑师：** 打完最后一幕的 Boss（进阶 10 时是第二个）后按继续，会和游戏一样进入建筑师事件（RunManager.EnterNextAct）：`event_choice`，`event_id: "THE_ARCHITECT"`，`dialogue` 里是到目前为止说过的台词，每句台词一个选项（它的按钮文字，或“回答”/“继续”），最后是“继续”，选了才赢下本局（TheArchitect.WinRun），这时才返回 `victory: true` 的 `game_over`。它的对话由档案里该角色的胜场和带种子的事件随机数决定，所以是准确的。战斗结束后会回到原事件的事件战斗（战痕累累的训练假人），领完战斗奖励后会回到事件：接着是它的结果页和事件自己的奖励（药水、遗物）。
- **商人？？？（Fake Merchant）：** 这个事件是一个小商店：返回 `shop` 决策并带 `"event":"FAKE_MERCHANT"`（只卖遗物；可用 `buy_relic`、`leave_room`）。`throwable_potions` 列出可以对商人 `use_potion` 的污浊药水，扔出后开始战斗（奖励包括商人的地毯？？？和没卖出的遗物）。
- **商店：** 遗物和药水带 `id`、`rarity`、`tips`（药水还有 `usage` / `target_type`）；卡牌带 `id` 和 `colorless`（商人的第二排）。买走的格子保留原来的序号，没有 `name`/`description`（值为 null，和其他嵌套的空字段一样被省略）。删牌和游戏里一样每次进店只能用一次：删过之后（包括领主阳伞的免费删牌，因此不会再提供一次付费删牌），或有禁止删牌的修改器时，`card_removal_available` 变为 false，下一家商店的价格加 25。
- **休息处：** 选项带游戏里的 `name` 和 `description`（遗物加成后的回复量、皇家枕头之类的额外说明、剩余锻炼次数、选项不可用的原因），休息还有 `heal_amount`。有微型帐篷时，选完一个选项后会再次给出剩下的选项；用过一个选项后会出现 `can_leave`（以及 `leave_room`）；在此之前只要还有可用的选项，`leave_room` 就会被拒绝，因为游戏里要选过一个选项才会出现"继续"按钮。用完最后一个选项后不会自动离开：`rest_site` 决策会以 `options: []` 再次返回，只有 `leave_room`（即"继续"按钮；仍然可以使用药水）。不可用的选项点不了（没有可升级的牌时的锻造、可移除的牌少于 2 张时的烹饪）；休息总是可用，所以只有所有选项都不可用时休息处才会软锁（见“软锁”）。
- **卡牌奖励：** `card_reward` 带有 `alternatives`（`[{index, id, name, after}]`：`Skip`、浮木的 `REROLL`、佩尔之翼的 `SACRIFICE`）；奖励不能跳过时（Draft）`can_skip` 为 false。选择 Skip 以外的选项用 `select_card_alternative {"alternative_index": i}`。`alternatives` 是界面当前显示的选项：浮木只能重抽一次，之后只剩 Skip（`REROLL` 消失）。佩尔之翼：每献祭两次获得一件遗物（`relic_obtained` 事件），`counter` 显示距离下一件遗物已献祭的次数（1，然后是 0）。卡牌奖励走游戏自己的选择流程（会记录历史、触发钩子）。跳过卡牌奖励（或取消删牌）会回到 `rewards` 界面，它仍可以再领，和游戏一样。选择界面会一直等你，`card_index` 无效时返回错误（界面保持打开）。
- **奖励：** 奖励条目带有 `description`；遗物奖励带 `id`/`rarity`，药水奖励的字段和药水栏一样。`special_card`（例如被偷窃草蜢偷走的牌，带 `card`）和 `card_removal`（禁忌魔典）奖励也会列出（领取删牌会弹出 `card_select`）。药水栏满时药水奖励留在界面上：可以先 `discard_potion` 再 `claim_reward`，或者不要它直接 `proceed`。拾起时会发放奖励的遗物（失物盒、小型扭蛋等）会打开嵌套的 `rewards` 界面，结束后回到外层界面，剩下的奖励还在。
- **游戏结束：** `game_over` 展示 NGameOverScreen 上显示的内容。`score`（游戏的 ScoreUtility）、`total_floor`（整局爬过的层数），失败时还有 `killed_by`（`type` 为 encounter/event，以及 `id`、`name`、`quote`：总结页上的遭遇台词）；放弃的一局（审判的坚持到底）`abandoned` 为 true。`banner`（“胜利……？”，或失败时随机的横幅，如“消亡”），`death_quote`（失败时第一页的随机台词）或 `victory_text`（对建筑师造成伤害的文字，以及解锁进阶的提示）。`score_lines`：按界面顺序的分数行，每行有 `key`（floorsClimbed、goldGained、elitesKilled、bossesSlain、ascension）、当前语言的 `text`（“攀爬楼层：33”）、原始数量 `amount`（33 层、511 金币、1 个精英……）以及界面上显示的 `points` 字符串（“+490”，进阶为“x1.5”）；击杀精英/Boss 行只在大于 0 时出现，进阶行只在进阶大于 0 时出现。`badges`：本局获得的徽章（ScoreUtility.GetBadges），每个有 `id`、`rarity`（bronze/silver/gold）、`title`、`description`。`score_progress`：解锁进度条（AnimateScoreBar）：`unlocks_remaining` 及其文字 `unlocks_remaining_text`（“剩余解锁：N”）、`visible`（所有解锁都已获得时为 false，不显示进度条），显示时还有 `score_before` / `threshold`、`score_added`（本局分数）、`score_after` / `threshold_after`、`progress_text`（“[310/500]”），进度条填满时有 `unlocked`、`unlock_text` 和 `unlocked_epoch`。`discoveries_header`（“发现”，有发现时才有）和 `discoveries`：按界面顺序的每一类（`kind` 为 cards、relics、potions、enemies、epochs）的 `count`、`names`、提示框的 `header` 和 `body`（游戏的文字：数量那一行加上前 10 个名字）。`main_menu_button` 是底部按钮的文字（发现了新历史节点时为“解锁”）。`score_breakdown` 为兼容保留，单位混用：`floors` 和 `gold` 是分数，`elites_killed`、`bosses_slain` 是数量，`ascension` 是进阶等级；另有 `floors_climbed`、`floors_points`、`gold_gained`、`gold_points`、`elites_killed_points`、`bosses_slain_points` 和 `ascension_multiplier`。和游戏一样，一局结束会在内存中更新档案进度（发现、历史节点、解锁进度条）；档案文件不会被写入。bin/sts2 和 play.py 会打印这个界面。
- **无界面防护：** 游戏逻辑里会直接调用界面对象（屏幕震动、事件房间布局等），在无界面环境下会抛异常，逻辑执行到一半就中断。这些调用会被改写为界面对象不存在时直接跳过：通常由 `setup.sh` 一次性完成（`tools/UiGuardPatcher`，并给 `lib/sts2.dll` 打上标记）；如果 DLL 没有标记，则在引擎启动时完成（每个进程约 1 秒）。`STS2_NO_UI_GUARD=1` 只会跳过启动时的改写（要完全不带防护运行，请换回游戏原版的 `lib/sts2.dll.original`）；`STS2_UI_GUARD_RUNTIME=1` 对未标记的 DLL 强制在启动时改写，`STS2_UI_GUARD_DUMP=<文件>` 会列出被改写的方法（测试用它比较两种实现）。
- **调试接口：** 设置 `STS2_DEBUG_COMMANDS=1` 后，`{"cmd":"__debug_stats"}` 返回 `heap_bytes`、`combat_ended_handlers`、`net_card_db_subscriptions`、`net_loading_handles`、`save_store_calls` 和 `map_pruning_verified`（内存泄漏修复所限制的各项大小，见 `tests/test_memory.py`）。如果启动时的初始化失败，错误会在第一次 `start_run`/`load_save` 时返回。
- **地图生成：** 游戏的 `MapPathPruning.FindMatchingSegments` 换成了结果完全相同的更快实现（地图和随机数消耗都一样）。`STS2_VERIFY_MAP_PRUNING=1` 会同时运行两者，结果不同就报错；`STS2_GAME_MAP_PRUNING=1` 保留游戏原版。（新实现不生成完整路径列表，跳过已经处理过的片段起点，并用值作为片段键；热启动的 `start_run` 快约 2.5 倍。）
- **动画停顿：** 游戏在熔合者（Amalgamator）里用 `Task.Delay` 做的实时停顿（移除两张牌和加入新牌之间的 300 毫秒，只有音效和震屏）会像 `Cmd.Wait` 一样被跳过，结果不变。设置 `STS2_KEEP_ANIMATION_DELAYS=1` 可保留。
- **地图移动：** `map_select` 只提供游戏地图上可以前往的节点：每一幕开始时只有起点（Neow / 先古之民）节点，之后是当前节点的子节点，最后是 Boss。羽翼之靴还有次数时（或开启“飞行”修改器时）会提供下一行的所有节点，这类选项带 `"free_travel": true`（会消耗一次次数），决策里也会有 `"free_travel_available": true`。除了 `map_select`，在其他地方发送 `select_map_node` 都会被拒绝。进阶 10（双重Boss）时最后一幕有两个 Boss：打完第一个后地图会开放第二个（`context.second_boss`），打完第二个才会进入建筑师事件（之后才是胜利）。进入新的一幕时不会回血，和游戏一样由起点的先古之民节点回血（进阶 2 起回复已损失生命的 80%）。
- **对局信息：** 每个决策的 `context` 包含 `act`、`act_name`、`floor`（本幕层数）、`total_floor`（顶栏显示的总层数）、`room_type`、`ascension`、`seed`、`run_time_seconds`（顶栏的计时器，即 `RunManager.RunTime`，按真实时间计，所以完全相同的两局也会不同）、`modifiers`（`[{id,name}]`，只在有修改器时出现；和顶栏图标一样使用修改器标题，例如 Character Cards 显示为“故障机器人卡牌”并带 `character`）、`boss`，以及双 Boss 幕的 `second_boss`（`{id, name}`，和顶栏一样用遭遇战名称）。`get_map` 返回完整的地图：`start`（第 0 行，图的根节点）、`rows`、`boss`、`second_boss`，每个节点都有 `children`、`visited`、`current` 和 `travelable`（去过的 `Unknown` 节点还带 `revealed_type`，即它实际是什么房间：`Monster`、`Elite`、`Treasure`、`Shop` 或 `Event`，地图会在 `?` 里画出对应图标）；另外还有 `visited_path`、`current_coord`、`ascension_effects`（`[{level,name,description}]`）、带说明的 `modifiers` 和 `run_time_seconds`。
- **遗物：** 玩家的遗物带 `id`，并且和游戏里遗物图标显示的一样带有 `counter`（例如钢笔尖的攻击计数、羽翼之靴剩余次数，只有显示计数的遗物才有）、`status`（`Active` 为发光，`Disabled` 为变灰，正常时省略）、`used_up`、`melted`、`wax` 和 `stack_count`（只在有值时出现）。蜡制遗物（玩具盒）的名字和游戏一样带前缀，例如“蜡制锚”。
- **药水：** 每瓶药水带 `can_use`，表示游戏的药水弹窗此刻是否能点“使用”。`AnyTime` 药水（鲜血药水、果汁等）可以在战斗外使用，并且在任何界面都会作为 `use_potion` 出现在 `legal_actions` 里；污浊药水只能在战斗中或商人处使用（获得 100 金币）。`CombatOnly` 药水在自己的战斗回合之外使用会被拒绝，使用失败或被取消时药水也不会被消耗。事件锁定药水时（如“药水的未来？”），`player.can_use_or_remove_potions` 为 false，`use_potion` 和 `discard_potion` 都会被拒绝。长者兰伟德和永恒之石也会锁定药水。在卡牌的选择界面打开时（如坚毅选择要消耗的牌）使用的药水会像游戏一样排在这张牌后面：它显示 `queued: true` 和 `can_use: false`，在喝下之前 `use_potion` 和 `discard_potion` 都不合法。越界的 `target_index` 或不存在的 `target_id` 会报错；不给目标时，能以自己为目标的药水（AnyPlayer）会和游戏一样用在自己身上。
- **存档：** `{"cmd":"write_continue_save","path":...}` / `{"cmd":"load_save","path":...}`。在房间内存档时写入的是游戏自己此刻的存档（进入节点时保存，或战斗胜利后保存），读档时会像游戏的“继续”一样重新进入同一个节点：遭遇战不变（已经打赢的战斗会重新给出奖励），也不能改走别的路线。在同一个房间里反复存档、读档也一样（引擎保留读入存档的独立副本，就像磁盘上的文件），首领房间也不例外。存档内容固定在游戏保存的那一刻：之后在房间里发生的事（例如从奖励中选的卡）不会写进去，与游戏的存档文件一致。建筑师的房间游戏从不存档（它通过 RunManager.EnterRoom 进入，也不是先古之民事件），所以那里的“继续”存档是最后一个 Boss 死亡时写下的：读档会回到 Boss 已结束的房间（空的奖励界面），按继续会再次进入建筑师事件，与游戏的“继续”一致。在 `map_select` 存档会恢复地图位置。游戏无法从中建立一局的存档（未知的角色或卡牌 id、缺少玩家等）会在拆掉当前这一局之前就以 `bad_request`（“Save is not loadable: ...”）拒绝，正在进行的这一局不受影响。
- **错误：** 所有错误都是 `{"type":"error","code":...,"message":...}`，并且只要有一局正在等待决策，就同时带上当前的 `decision` 和 `legal_actions`（查询命令和格式错误的行也一样）。每一行输入都恰好得到一个回复：空行或只有空白的行也返回 `bad_request`。引擎不认识的名字在错误里最多回显 64 个字符；超过 64 个字符的 `seed`（游戏的种子是 12 位）返回 `bad_request`。错误码：`bad_request`（格式或字段错误、类型错误、缺少必填字段）、`unknown_command`、`unknown_action`、`illegal_action`（当前不合法，包括选牌处理时被拒绝，例如选的张数不对）、`unknown_character`、`no_run`（还没有开局）、`io_error`（存档或档案文件无法读写）、`debug_disabled`、`flow_failed`、`exception`、`engine_stuck`、`engine_timeout`、`restart_required`。如果引擎死锁、战斗无法推进，或者无法给出房间的奖励（战斗奖励生成失败、宝箱遗物无法提供或结算），会返回 `"code":"engine_stuck"`（之后一直返回它），不再伪装成判负，也不会不声不响地丢掉奖励离开房间。遇到时放弃这一局，重新开局。
- **崩溃：** stdout 只输出协议 JSON（库和日志输出都走 stderr）。进程因未处理异常退出前，会先写一行 `{"type":"fatal","message":...,"stack_trace":...}`。
- `python3 scripts/bench.py [--reuse] [--jobs N] [--out f.json] [--compare base.json]` 用固定种子跑基准，为每局输出一个轨迹指纹（忽略 null 字段），并给出 start_run 和每步延迟的分位数，用来检查引擎改动有没有改变游戏行为。

## 游戏日志

每局游戏会自动记录到 `logs/` 目录下的 JSONL 文件中，包含每一步的游戏状态和操作，附带时间戳。超过 7 天的旧日志会自动清理。

```bash
python3 python/play.py --no-log    # 关闭日志
```

**提交 bug 报告时，请附上 `logs/` 中对应的日志文件** — 它包含了复现问题所需的完整游戏步骤。

## 架构

```
你的代码 (Python / JS / LLM)
    │  JSON stdin/stdout
    ▼
src/Sts2Headless (C#)
    │  RunSimulator.cs
    ▼
sts2.dll (游戏引擎, IL patched + 无界面防护, 由 setup.sh / tools/UiGuardPatcher 完成)
  + src/GodotStubs (替代 GodotSharp.dll)
  + Harmony patches (本地化、无界面空操作、地图剪枝)
```

</details>
