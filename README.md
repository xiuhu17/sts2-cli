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
./setup.sh      # copies DLLs from Steam → IL patches → builds
```

Or just run `python3 python/play.py` — it auto-detects and sets up on first run.

Tested with **v0.111.0 (Steam public-beta, build 24724944)**. After updating the
installed game in Steam, rerun `./setup.sh` to refresh the engine DLLs, patches,
and official English/Chinese localization, then rebuild. Other game versions
may require adapter changes.

For a compatibility check, run `python3 python/play_full_run.py 5 Ironclad`
(repeat for Silent, Defect, Regent, and Necrobinder). A completed run reaches
victory or defeat; crashes, stalls, and timeouts return a nonzero exit code.

## Play

```bash
python3 python/play.py                        # interactive (Chinese)
python3 python/play.py --lang en              # interactive (English)
python3 python/play.py --ascension 10         # Ascension 10
python3 python/play.py --character Silent      # play as Silent
```

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
  Reward:  card index / s (skip)
  Rest:    option index
  Event:   option index / leave
  Shop:    c0 (card) / r0 (relic) / p0 (potion) / rm (remove) / leave
```

## For AI agents: the `sts2` shell client (recommended)

An LLM agent that plays through a Bash tool should use `bin/sts2`: one short command
per step, a compact text view with numbered legal actions, and all rules text kept.
The first call starts a background daemon that holds one engine process (unix socket
under `$TMPDIR`; restarted and the run replayed if the engine dies; stopped after 30
minutes idle, `STS2_IDLE_TIMEOUT`), so every later call is a ~20 ms round-trip
(15 ms of it is Python start-up; measured on an M-series Mac).

```bash
bin/sts2 start --character Ironclad --seed abc        # [--ascension N] [--lang zh] [--profile PATH]
bin/sts2 state                     # current decision + numbered actions
bin/sts2 do 3                      # take action [3]
bin/sts2 do "play Bash E0; play Strike E0; end"   # batch: stops at the first error, or when a
                                   # step no longer fits the decision (e.g. a card selection popped up)
bin/sts2 deck | relics | potions | piles [draw] | map | card N | log [N]
bin/sts2 --json state              # raw engine JSON instead of text
bin/sts2 help                      # all verbs
bin/sts2 stop
```

Each reply starts with what happened since the previous decision (cards played and
drawn, damage, block, powers, enemy moves, relic triggers, gold/deck/relic changes),
then the decision: in combat the enemies with intents and powers, the hand with card
text, energy, orbs/stars/Osty, piles; for card selections the purpose
(upgrade/remove/transform/enchant/discard/exhaust/choose), the game's prompt, the source,
whether it can be cancelled, and upgrade/enchant/transform previews. Replies stay under
8 KB. Steps in a batch are read against the state at the moment they run, so refer to
cards by name (`play Strike`) or re-check indices. `--lang zh` uses the game's official
Chinese text. Debug commands are never enabled in the daemon. Set `STS2_SESSION` to run
several independent games.

`agent/sts2_bridge.py` (HTTP, one JSON command per POST) is still available; it now
restarts a dead engine and replays the run, answers bad bodies with a JSON error,
serves `GET /state` and `GET /help`, and its `--compact` mode keeps all descriptions.

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

Each command returns a JSON decision point (`map_select` / `combat_play` / `card_reward` / `card_select` / `bundle_select` / `rest_site` / `event_choice` / `shop` / `crystal_sphere` / `game_over`, plus `rewards` / `treasure_relic` with `manual_rewards`). All names are in English.

- **Many runs per process:** send `start_run` (or `load_save`) again to begin a new run; the previous one is torn down. This skips the ~0.6s engine start-up, so batch/RL workloads should keep one process per worker. `python/engine.py` has a small `Engine` wrapper (`dotnet bin/.../Sts2Headless.dll` directly, bounded reads, stderr drained).
- **Legal actions:** every decision includes `legal_actions`, a list of ready-to-send actions such as `{"action":"play_card","args":{"card_index":0,"target_index":1}}`. For multi-card prompts there is one entry `{"action":"select_cards","choose":{"from":[0,1,2],"min":1,"max":2}}`; send `{"indices":"0,2"}` (or `[0,2]`). The Crystal Sphere minigame has one entry `{"action":"crystal_sphere_click","choose_cell":{"cells":[[x,y],...],"tools":["big","small"]}}`; send `{"x":..,"y":..,"tool":..}`. In general, an entry with a `choose*` key instead of `args` needs the client to fill in the arguments. Potions whose `usage` is `Automatic` (e.g. Fairy in a Bottle) are never listed for `use_potion` and are rejected if sent. Invalid input is rejected with an error instead of being silently ignored (unreachable map nodes, locked/out-of-range options, bad card indices or counts). `scripts/fuzz_legal.py` plays random legal actions and fails on any error.
- **Strict actions:** an action that is not legal for the current decision is rejected — `{"type":"error","code":"illegal_action","message":"'leave_room' is not allowed now: a choice is still required here. Current decision: event_choice. Legal: choose_option(option_index=0|1|2)","decision":"event_choice","legal_actions":[...]}`. `leave_room` can't skip events or rest sites, and `skip_select` only works when the selection has `min_select` 0 or is `cancelable`. Every action error carries `decision` and `legal_actions`; unknown actions get a "did you mean" hint; `args` indices must be integers (numeric strings are accepted), and `target_index` must name an alive enemy. Potions follow the game's Use button: CombatOnly potions only on your combat turn with no card selection open (outside that they are rejected instead of being lost); AnyTime potions (Blood Potion, Fruit Juice, ...) everywhere, and `legal_actions` lists them at every decision. Malformed lines (not an object, missing `cmd`, `ascension` outside 0-10, ...) get `"code":"bad_request"`. Errors carry no .NET stack traces unless `STS2_DEBUG=1`. If a game flow started by an action fails inside the game, the reply is `"code":"flow_failed"` with the resulting decision in `state`.
- **Card selections:** `card_select` says what it is for, as the game's screen does: `purpose` (`upgrade` / `remove` / `transform` / `enchant` / `discard` / `exhaust` / `choose`), `prompt` (the game's text, e.g. "Choose a card to Exhaust."), `cancelable` (the screen has a cancel/skip button: rest-site Smith, shop removal, ...; then `skip_select` is legal and backs out), `screen` (`hand` / `deck` / `grid` / `draw` / `discard` / `exhaust` / `choose_a_card` / `reward_grid`) and `source` (`{name, kind}` of the card, relic, potion, power or event that asked). Enchant screens add `enchantment {id, name, amount, description}` and a per-card `after_enchant`; transforms with a fixed result add `transforms_to`. During combat, `combat` holds the battlefield (energy, hand, enemies with intents, powers, orbs, pile counts) like the game's peek button.
- **Events log:** each decision has `events`, what happened since the previous decision, in order: `turn` markers, `card_played`, `draw`, `damage` (`source`, `target`, `amount`, `blocked`, `killed`), `block`, `power`, `enemy_move` (with the intents it showed), `card_added`, `discard`, `exhaust`, `orb_channeled`, `potion_used`, `relic` (a relic triggered), `enemy_died`, and run-level changes `gold`, `hp`, `max_hp`, `deck_added` / `deck_removed` / `deck_changed`, `relic_obtained`, `potion_obtained` / `potion_lost`. Capped at 80 entries.
- **Queries (never advance the game):** `{"cmd":"get_state"}` re-sends the current decision; `get_deck` (`"upgrades":true` adds upgrade previews), `get_relics`, `get_potions`, `get_piles` (`"pile":"draw"|"discard"|"exhaust"|"hand"`; draw pile sorted, order hidden), `get_card` (`"deck_index":N` / `"hand_index":N` / `"id":"BASH"`, `"upgraded":true`), `get_map`, `help`.
- **Lean decisions:** `"omit_deck": true` on `start_run` / `load_save` leaves `player.deck` out of every decision (`player.deck_size` and `player.deck_version`, which changes whenever the deck does, stay; `get_deck` returns the cards). Default unchanged. Responses list `type`, `decision`, `legal_actions` first, so a truncated line still shows them. Shop items also have `price` (gold) and cards `energy_cost`, since `cost` means gold there but energy everywhere else.
- **Debug commands:** `set_player`, `enter_room` and `set_draw_order` change or reveal state a player can't, so they return `"code":"debug_disabled"` unless the engine runs with `STS2_DEBUG=1` (or `--debug`).
- **Text:** every `description` (cards, relics, potions, powers, events) is the game's own text with numbers filled in, as shown in the game UI — card numbers follow live combat values (Strength, Weak, ...). Icons become text (`1 Energy`, `★`). Cards in hand / rewards / selections / shop, potions and offered relics also carry `tips`: the secondary tooltips the game shows next to them (keyword and power explanations such as Vulnerable or Exhaust).
- **Pile contents:** `combat_play` includes `draw_pile` / `discard_pile` / `exhaust_pile` (grouped as `{name, count, ...}`, sorted by name — draw order is not revealed).
- **Custom mode:** `start_run` accepts `"modifiers": ["SEALED_DECK", "MIDAS", ...]` (an unknown name returns the list of valid ones) and `"game_mode": "standard"|"custom"|"daily"` (default `custom` when modifiers are given). `CHARACTER_CARDS` needs the character, as in the custom-run screen: `"CHARACTER_CARDS:Silent"`. Deck-building modifiers (Draft, Sealed Deck, Insanity) add their Neow option as in the game. The daily challenge's seed and modifiers are generated by the game's UI from the server date and aren't reproduced; pass them explicitly with `"game_mode":"daily"`.
- **Manual rewards:** `{"cmd":"start_run", ..., "manual_rewards": true}` (also on `load_save`). After combat you get a `rewards` decision listing each reward (`gold` / `relic` / `potion` / `card`, with a stable `index`): `claim_reward {"reward_index": i}` takes one (a card reward opens the usual `card_reward` prompt), `proceed` leaves the rest. Treasure chests give a `treasure_relic` decision: `select_treasure_relic {"relic_index": i}` or `skip_treasure_relic`. Rewards handed out by events (e.g. Neow's options) also arrive as a `rewards` decision with `"source":"event"`. Without the flag, gold/relics/potions and the chest's first relic are collected automatically as before.
- **Crystal Sphere:** the event's minigame is a `crystal_sphere` decision (`rows` with `?` hidden / `.` empty / item letters, `hidden_cells`, `items_seen`, `divinations_left`); reveal with `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` (big = 3x3).
- **Headless UI guard:** game logic that calls UI objects inline (screen shake, event-room layout, ...) would throw headless and abort halfway. At startup (~1s once per process) those call sites are rewritten to skip when the UI object is absent; set `STS2_NO_UI_GUARD=1` to disable.
- **Errors:** `{"type":"error","message":...}` (plus `code` for the kinds above). If the engine deadlocks and can't advance combat, it returns `"code":"engine_stuck"` (and keeps returning it) instead of a fake defeat — abandon that run and start a new one.
- **Crashes:** stdout carries only protocol JSON (library/log output goes to stderr). If the process is about to die from an unhandled exception it first writes `{"type":"fatal","message":...,"stack_trace":...}`.
- `python3 scripts/bench.py [--reuse] [--jobs N]` benchmarks fixed seeds and prints a trajectory fingerprint per run, for checking that engine changes don't alter game behaviour.

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
sts2.dll (game engine, IL patched)
  + src/GodotStubs (replaces GodotSharp.dll)
  + Harmony patches (localization)
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
./setup.sh      # 从 Steam 复制 DLL → IL patch → 编译
```

或者直接运行 `python3 python/play.py`，首次会自动完成 setup。

已验证 **v0.111.0（Steam public-beta，构建 24724944）**。通过 Steam 更新游戏后，
请重新运行 `./setup.sh`，同步游戏 DLL、补丁和官方中英文文本并重新编译。
其他游戏版本可能需要修改适配代码。

可运行 `python3 python/play_full_run.py 5 Ironclad` 检查兼容性，并依次替换为
Silent、Defect、Regent、Necrobinder。“完成”指正常胜利或死亡；崩溃、卡住和超时
会返回非零退出码。

## 玩

```bash
python3 python/play.py                        # 中文交互模式
python3 python/play.py --lang en              # English
python3 python/play.py --ascension 10         # 渐进难度 10
python3 python/play.py --character Silent      # 选择静默猎手
```

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
  奖励:    输入卡牌编号 / s 跳过
  休息:    输入选项编号
  事件:    输入选项编号 / leave 离开
  商店:    c0 买卡 / r0 买遗物 / p0 买药水 / rm 移除 / leave 离开
```

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
bin/sts2 start --character Ironclad --seed abc --lang zh   # [--ascension N] [--profile PATH]
bin/sts2 state                     # 当前决策 + 编号的可选操作
bin/sts2 do 3                      # 执行第 [3] 个操作
bin/sts2 do "play Bash E0; play Strike E0; end"   # 批量：遇到第一个错误、或某一步不再适用于当前决策
                                   # （例如弹出了选牌）时停止
bin/sts2 deck | relics | potions | piles [draw] | map | card N | log [N]
bin/sts2 --json state              # 输出引擎原始 JSON
bin/sts2 help                      # 全部指令
bin/sts2 stop
```

每次返回先列出上次决策以来发生的事（打出和抽到的牌、伤害、格挡、能力、敌人行动、遗物触发、金币/牌组/遗物变化），然后是当前决策：战斗中显示敌人的意图和能力、带卡牌说明的手牌、能量、充能球/辉星/奥斯提、牌堆；选牌时显示用途（升级/移除/变化/附魔/丢弃/消耗/选择）、游戏的提示语、来源、能否取消，以及升级/附魔/变化预览。每次输出都小于 8 KB。批量里的每一步都按执行那一刻的状态解析，所以建议用牌名（`play Strike`）或重新确认编号。`--lang zh` 使用游戏官方中文。守护进程里永远不会开启调试命令。设置 `STS2_SESSION` 可以同时进行多局互不影响的游戏。

`agent/sts2_bridge.py`（HTTP，每次 POST 一条 JSON 命令）仍然可用：现在引擎退出后会自动重启并重放本局，非法请求体会返回 JSON 错误，提供 `GET /state` 和 `GET /help`，`--compact` 模式也不再删除说明文字。

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

每个命令返回一个 JSON decision point（`map_select` / `combat_play` / `card_reward` / `card_select` / `bundle_select` / `rest_site` / `event_choice` / `shop` / `crystal_sphere` / `game_over`；开启 `manual_rewards` 后还有 `rewards` / `treasure_relic`），所有名称为英文。

- **一个进程跑多局：** 再发一次 `start_run`（或 `load_save`）就会结束上一局、开始新的一局，省掉约 0.6 秒的引擎启动。批量跑或 RL 建议每个 worker 只开一个进程。`python/engine.py` 里有一个简单的 `Engine` 封装：直接用 `dotnet` 运行编译好的 dll，读取有超时，stderr 会被读走。
- **合法操作：** 每个决策都带一个 `legal_actions` 列表，列出可以直接发送的操作，例如 `{"action":"play_card","args":{"card_index":0,"target_index":1}}`。需要选多张牌时只有一条 `{"action":"select_cards","choose":{"from":[0,1,2],"min":1,"max":2}}`，发送时用 `{"indices":"0,2"}`（或 `[0,2]`）。Crystal Sphere 小游戏也只有一条 `{"action":"crystal_sphere_click","choose_cell":{"cells":[[x,y],...],"tools":["big","small"]}}`，发送时用 `{"x":..,"y":..,"tool":..}`。总的规则是：条目里没有 `args`、而是带 `choose*` 键时，需要客户端自己填参数。`usage` 为 `Automatic` 的药水（例如 Fairy in a Bottle）不会出现在 `use_potion` 里，直接发送也会被拒绝。非法输入会直接报错，不再被悄悄忽略，比如走不到的地图节点、锁定或越界的选项、错误的卡牌序号或张数。`scripts/fuzz_legal.py` 会随机执行合法操作，只要收到报错就算失败。
- **严格校验操作：** 当前决策不允许的操作会被拒绝，返回 `{"type":"error","code":"illegal_action","message":"'leave_room' is not allowed now: ... Current decision: event_choice. Legal: choose_option(option_index=0|1|2)","decision":"event_choice","legal_actions":[...]}`。`leave_room` 不能跳过事件或休息处；只有 `min_select` 为 0 或 `cancelable` 为 true 的选牌才能 `skip_select`。所有操作错误都带有 `decision` 和 `legal_actions`；未知操作会提示"did you mean"；`args` 里的序号必须是整数（数字字符串也可以），`target_index` 必须指向存活的敌人。药水与游戏的“使用”按钮一致：仅限战斗的药水只能在自己的战斗回合、且没有打开选牌界面时使用（其他时候会被拒绝，而不是被白白丢掉）；随时可用的药水（鲜血药水、果汁等）任何时候都能用，`legal_actions` 在每个决策都会列出。格式错误的输入（不是对象、缺少 `cmd`、`ascension` 不在 0-10 等）返回 `"code":"bad_request"`。除非设置 `STS2_DEBUG=1`，错误里不再带 .NET 堆栈。如果某个操作触发的游戏流程在游戏内部失败，会返回 `"code":"flow_failed"`，`state` 里是之后的决策。
- **选牌：** `card_select` 会像游戏界面一样说明用途：`purpose`（`upgrade` 升级 / `remove` 移除 / `transform` 变化 / `enchant` 附魔 / `discard` 丢弃 / `exhaust` 消耗 / `choose` 选择）、`prompt`（游戏的提示文字，例如"选择1张牌来消耗。"）、`cancelable`（界面上有取消/跳过按钮，例如休息处锻造、商店移除卡牌；此时 `skip_select` 合法，用于退出）、`screen`（`hand` / `deck` / `grid` / `draw` / `discard` / `exhaust` / `choose_a_card` / `reward_grid`）和 `source`（发起选择的卡牌、遗物、药水、能力或事件 `{name, kind}`）。附魔界面另有 `enchantment {id, name, amount, description}` 和每张牌的 `after_enchant`；结果固定的变化有 `transforms_to`。战斗中 `combat` 字段给出战场（能量、手牌、带意图的敌人、能力、充能球、牌堆张数），相当于游戏里的"查看战场"按钮。
- **事件日志：** 每个决策带 `events`，按顺序列出上次决策以来发生的事：`turn` 回合标记、`card_played`、`draw`、`damage`（`source`、`target`、`amount`、`blocked`、`killed`）、`block`、`power`、`enemy_move`（带当时显示的意图）、`card_added`、`discard`、`exhaust`、`orb_channeled`、`potion_used`、`relic`（遗物触发）、`enemy_died`，以及整局层面的变化 `gold`、`hp`、`max_hp`、`deck_added` / `deck_removed` / `deck_changed`、`relic_obtained`、`potion_obtained` / `potion_lost`。最多 80 条。
- **查询（不推进游戏）：** `{"cmd":"get_state"}` 重新发送当前决策；`get_deck`（`"upgrades":true` 附带升级预览）、`get_relics`、`get_potions`、`get_piles`（`"pile":"draw"|"discard"|"exhaust"|"hand"`；抽牌堆已排序，不暴露顺序）、`get_card`（`"deck_index":N` / `"hand_index":N` / `"id":"BASH"`，`"upgraded":true`）、`get_map`、`help`。
- **精简决策：** 在 `start_run` / `load_save` 里加 `"omit_deck": true`，每个决策都不再附带 `player.deck`（保留 `player.deck_size` 和随牌组变化而变化的 `player.deck_version`；需要时用 `get_deck`）。默认行为不变。返回的 JSON 把 `type`、`decision`、`legal_actions` 放在最前面，被截断时也能看到。商店物品另有 `price`（金币），卡牌另有 `energy_cost`，因为商店里的 `cost` 指金币，其他地方指能量。
- **调试命令：** `set_player`、`enter_room`、`set_draw_order` 会修改或暴露玩家无法得知的状态，因此除非引擎以 `STS2_DEBUG=1`（或 `--debug`）启动，否则返回 `"code":"debug_disabled"`。
- **文字：** 所有 `description`（卡牌、遗物、药水、能力、事件）都是游戏自己的文字，数字已经填好，和游戏界面上看到的一样。卡牌数值会随战斗中的力量、虚弱等实时变化。图标会转成文字（`1点能量`、`★`）。手牌、奖励、选牌、商店里的卡牌，以及药水和奖励中的遗物，还带有 `tips`：也就是游戏在它们旁边显示的二级提示，比如"易伤""消耗"等关键词和能力的解释。
- **牌堆内容：** `combat_play` 里有 `draw_pile` / `discard_pile` / `exhaust_pile`，按 `{name, count, ...}` 分组、按名字排序，不暴露抽牌顺序。
- **自定模式：** `start_run` 支持 `"modifiers": ["SEALED_DECK", "MIDAS", ...]`（名字写错时会返回所有可用的修改器）和 `"game_mode": "standard"|"custom"|"daily"`（传了修改器时默认为 `custom`）。`CHARACTER_CARDS` 需要指定角色，和自定模式界面一样：`"CHARACTER_CARDS:Silent"`。会重建牌组的修改器（Draft、Sealed Deck、Insanity）会像游戏里一样在 Neow 加上对应选项。每日挑战的种子和修改器是游戏界面按服务器日期生成的，这里不复现；可以用 `"game_mode":"daily"` 把它们显式传进来。
- **手动领奖励：** 在 `start_run`（或 `load_save`）里加 `"manual_rewards": true`。战斗结束后会返回 `rewards` 决策，列出每个奖励（`gold` / `relic` / `potion` / `card`，`index` 固定不变）。`claim_reward {"reward_index": i}` 领取其中一个，领卡牌奖励会弹出平常的 `card_reward` 选择；`proceed` 放弃剩下的。宝箱会返回 `treasure_relic` 决策，用 `select_treasure_relic {"relic_index": i}` 选择，或用 `skip_treasure_relic` 跳过。事件发放的奖励（例如 Neow 的选项）也会以 `rewards` 决策出现，并带有 `"source":"event"`。不加这个参数时，金币、遗物、药水和宝箱的第一个遗物仍按原来的方式自动领取。
- **Crystal Sphere：** 这个事件的小游戏是 `crystal_sphere` 决策，包含 `rows`（`?` 未揭开、`.` 空、字母表示物品）、`hidden_cells`、`items_seen` 和 `divinations_left`。用 `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` 揭开格子，big 是 3x3 范围。
- **无界面防护：** 游戏逻辑里会直接调用界面对象（屏幕震动、事件房间布局等），在无界面环境下会抛异常，逻辑执行到一半就中断。启动时（每个进程一次，约 1 秒）会把这些调用改写为界面对象不存在时直接跳过。设置 `STS2_NO_UI_GUARD=1` 可以关闭。
- **错误：** `{"type":"error","message":...}`（上面几类错误另带 `code`）。如果引擎死锁、战斗无法推进，会返回 `"code":"engine_stuck"`（之后一直返回它），不再伪装成判负。遇到时放弃这一局，重新开局。
- **崩溃：** stdout 只输出协议 JSON（库和日志输出都走 stderr）。进程因未处理异常退出前，会先写一行 `{"type":"fatal","message":...,"stack_trace":...}`。
- `python3 scripts/bench.py [--reuse] [--jobs N]` 用固定种子跑基准，并为每局输出一个轨迹指纹，用来检查引擎改动有没有改变游戏行为。

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
sts2.dll (游戏引擎, IL patched)
  + src/GodotStubs (替代 GodotSharp.dll)
  + Harmony patches (本地化)
```

</details>
