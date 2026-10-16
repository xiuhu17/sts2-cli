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
- **Text:** every `description` (cards, relics, potions, powers, events) is the game's own text with numbers filled in, as shown in the game UI — card numbers follow live combat values (Strength, Weak, ...). Icons become text (`1 Energy`, `★`); an energy icon used as a unit after a number reads as `Energy` (`costs 0 Energy`, `Gain 4 Energy`). Cards in hand / rewards / selections / shop, potions and offered relics also carry `tips`: the secondary tooltips the game shows next to them (keyword and power explanations such as Vulnerable or Exhaust).
- **Pile contents:** `combat_play` includes `draw_pile` / `discard_pile` / `exhaust_pile`, each card with its `description` and `keywords`, like the game's pile screens. The draw pile is grouped as `{name, count, ...}` and sorted by (rarity, id) like the game, so draw order is not revealed. The discard and exhaust piles list one entry per card (`count` 1) in their real order, as the game shows them.
- **Cards:** every card entry (hand, piles, deck, rewards, card_select, bundles, shop) is named like the game's title (`Strike+`, `Searing Blow+3`) and has `upgraded`, `upgrade_level` (when upgraded or multi-upgrade), `max_upgrade_level` (multi-upgrade cards) and `rarity`. X-energy cards have `costs_x: true` (`cost` stays 0). Cards with a star cost (Regent) have `star_cost` (0 or more), and X-star cards have `star_cost_x: true`. Cards that can never be played have `unplayable: true`. In hand, `star_cost` includes modifiers (Void Form, Brilliant Scarf), `x_value` is what X is now (energy, or stars for X-star cards), and `can_play` is the game's own check. When it's false, `unplayable_reasons` lists why (`EnergyCostTooHigh`, `StarCostTooHigh`, `HasUnplayableKeyword`, `BlockedByHook`, `BlockedByCardLogic`, `NoLivingAllies`) and `blocked_by` names the blocker (e.g. Normality). `glow` is `gold`/`red` when the game highlights the card. `legal_actions` follows `can_play` alone. Shop cards, relics and potions also have `id`.
- **Enemies and targets:** `enemies` lists the targetable enemies; `index` is the `target_index` to send. Each enemy also has a `monster_id` and a `combat_id` that stays the same for the whole combat: send it as `target_id` instead of `target_index` in `play_card` / `use_potion`, so a kill mid-turn can't shift your target. Enemies that are still in combat but can't be targeted (a Decimillipede segment waiting to reattach) appear in `inactive_enemies` with `reviving: true` and their pending move. Intents have `title` and `description` (the game's intent tooltip, e.g. "This enemy intends to give you 2 Status cards."), and Status intents have `card_count`. Powers (player, enemies, Osty) have `id`, `name`, `description`, `amount` and `type` (`Buff`/`Debuff`). Against the Kaiser Crab, `player_facing` is `Left`/`Right` and each crab has `behind_player` (its attacks deal x1.5; targeting it turns you around). `damage_by_target` rows have `target_id`, use CalculatedHits for `repeat`/`total_damage` (Tear Asunder, Finisher, ...), and also cover Osty attacks (`dealer: "osty"`) and random-target cards. `osty` has `powers`. Defect's `orbs` / `orb_slots` are present whenever the player has orb slots (`orbs` is an empty list when none are channeled); each orb has `description` and `evokes_next` (index 0 is the rightmost orb, evoked first).
- **Potions:** a potion's `usable` is false when the game would grey out "Use" (e.g. Foul Potion outside a shop). An out-of-range `target_index` or unknown `target_id` is an error. A use the game cancels returns an error and keeps the potion (it used to be discarded). Outside combat, Foul Potion can be thrown at the merchant in a shop for 100 gold.
- **Custom mode:** `start_run` accepts `"modifiers": ["SEALED_DECK", "MIDAS", ...]` (an unknown name returns the list of valid ones) and `"game_mode": "standard"|"custom"|"daily"` (default `custom` when modifiers are given). Deck-building modifiers (Draft, Sealed Deck, Insanity) add their Neow option as in the game. The daily challenge's seed and modifiers are generated by the game's UI from the server date and aren't reproduced; pass them explicitly with `"game_mode":"daily"`.
- **Manual rewards:** `{"cmd":"start_run", ..., "manual_rewards": true}` (also on `load_save`). After combat you get a `rewards` decision listing each reward (`gold` / `relic` / `potion` / `card`, with a stable `index`): `claim_reward {"reward_index": i}` takes one (a card reward opens the usual `card_reward` prompt), `proceed` leaves the rest. Treasure chests give a `treasure_relic` decision: `select_treasure_relic {"relic_index": i}` or `skip_treasure_relic`. Rewards handed out by events (e.g. Neow's options) also arrive as a `rewards` decision with `"source":"event"`. Without the flag, gold/relics/potions and the chest's first relic are collected automatically as before.
- **Crystal Sphere:** the event's minigame is a `crystal_sphere` decision (`rows` with `?` hidden / `.` empty / item letters, `hidden_cells`, `items_seen`, `divinations_left`); reveal with `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` (big = 3x3).
- **Headless UI guard:** game logic that calls UI objects inline (screen shake, event-room layout, ...) would throw headless and abort halfway. At startup (~1s once per process) those call sites are rewritten to skip when the UI object is absent; set `STS2_NO_UI_GUARD=1` to disable.
- **Errors:** `{"type":"error","message":...}`. If the engine deadlocks and can't advance combat, it returns `"code":"engine_stuck"` (and keeps returning it) instead of a fake defeat — abandon that run and start a new one.
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
- **文字：** 所有 `description`（卡牌、遗物、药水、能力、事件）都是游戏自己的文字，数字已经填好，和游戏界面上看到的一样。卡牌数值会随战斗中的力量、虚弱等实时变化。图标会转成文字（`1点能量`、`★`）；跟在数字后面当单位用的能量图标只写成"点能量"（`耗能变为0点能量`、`获得4点能量`）。手牌、奖励、选牌、商店里的卡牌，以及药水和奖励中的遗物，还带有 `tips`：也就是游戏在它们旁边显示的二级提示，比如"易伤""消耗"等关键词和能力的解释。
- **牌堆内容：** `combat_play` 里有 `draw_pile` / `discard_pile` / `exhaust_pile`，每张牌都带 `description` 和 `keywords`，和游戏里的牌堆界面一样。抽牌堆按 `{name, count, ...}` 分组，并像游戏一样按（稀有度, id）排序，不暴露抽牌顺序。弃牌堆和消耗堆每张牌一条（`count` 为 1），按真实顺序排列，和游戏显示的一样。
- **卡牌：** 所有卡牌条目（手牌、牌堆、牌组、奖励、card_select、卡包、商店）的名字和游戏标题一致（`Strike+`、`Searing Blow+3`），并带有 `upgraded`、`upgrade_level`（已升级或可多次升级时）、`max_upgrade_level`（可多次升级的牌）和 `rarity`。X 费牌有 `costs_x: true`（`cost` 仍为 0）。有星辰费用的牌（储君）有 `star_cost`（0 或以上），X 星辰费用的牌有 `star_cost_x: true`。永远无法打出的牌有 `unplayable: true`。手牌里的 `star_cost` 已包含修正（虚空形态、Brilliant Scarf），`x_value` 是当前 X 的值（能量；X 星辰牌则为星辰数），`can_play` 就是游戏自己的判断。为 false 时，`unplayable_reasons` 列出原因（`EnergyCostTooHigh`、`StarCostTooHigh`、`HasUnplayableKeyword`、`BlockedByHook`、`BlockedByCardLogic`、`NoLivingAllies`），`blocked_by` 给出阻止者的名字（例如 Normality）。游戏高亮的牌带 `glow`（`gold`/`red`）。`legal_actions` 只看 `can_play`。商店里的卡牌、遗物和药水也带 `id`。
- **敌人与目标：** `enemies` 列出可以选为目标的敌人，`index` 就是要发送的 `target_index`。每个敌人还有 `monster_id`，以及整场战斗不变的 `combat_id`：在 `play_card` / `use_potion` 里可以用 `target_id` 代替 `target_index`，这样回合中途有敌人死亡也不会打错目标。仍在战斗中但不能被选为目标的敌人（例如等待重新接上的 Decimillipede 节段）放在 `inactive_enemies` 里，带 `reviving: true` 和它接下来的行动。意图带 `title` 和 `description`（游戏里的意图提示，例如"该敌人打算给你 2 张状态牌"），状态牌意图还带 `card_count`。能力（玩家、敌人、Osty）带 `id`、`name`、`description`、`amount` 和 `type`（`Buff`/`Debuff`）。对帝皇蟹时，`player_facing` 为 `Left`/`Right`，每只蟹有 `behind_player`（它的攻击伤害 x1.5；以它为目标会让你转身）。`damage_by_target` 每行带 `target_id`，`repeat`/`total_damage` 会用 CalculatedHits（撕裂、终结技等），也覆盖 Osty 的攻击（`dealer: "osty"`）和随机目标的牌。`osty` 带 `powers`。故障机器人只要有充能球栏位就一定有 `orbs` / `orb_slots`（没有充能球时 `orbs` 是空列表）；每个充能球带 `description` 和 `evokes_next`（index 0 是最右边、最先被激发的充能球）。
- **药水：** 游戏会把"使用"按钮置灰时（例如不在商店里的 Foul Potion），药水的 `usable` 为 false。越界的 `target_index` 或不存在的 `target_id` 会报错。被游戏取消的使用会报错并保留药水（以前会被丢掉）。战斗外在商店里可以把 Foul Potion 扔向商人，获得 100 金币。
- **自定模式：** `start_run` 支持 `"modifiers": ["SEALED_DECK", "MIDAS", ...]`（名字写错时会返回所有可用的修改器）和 `"game_mode": "standard"|"custom"|"daily"`（传了修改器时默认为 `custom`）。会重建牌组的修改器（Draft、Sealed Deck、Insanity）会像游戏里一样在 Neow 加上对应选项。每日挑战的种子和修改器是游戏界面按服务器日期生成的，这里不复现；可以用 `"game_mode":"daily"` 把它们显式传进来。
- **手动领奖励：** 在 `start_run`（或 `load_save`）里加 `"manual_rewards": true`。战斗结束后会返回 `rewards` 决策，列出每个奖励（`gold` / `relic` / `potion` / `card`，`index` 固定不变）。`claim_reward {"reward_index": i}` 领取其中一个，领卡牌奖励会弹出平常的 `card_reward` 选择；`proceed` 放弃剩下的。宝箱会返回 `treasure_relic` 决策，用 `select_treasure_relic {"relic_index": i}` 选择，或用 `skip_treasure_relic` 跳过。事件发放的奖励（例如 Neow 的选项）也会以 `rewards` 决策出现，并带有 `"source":"event"`。不加这个参数时，金币、遗物、药水和宝箱的第一个遗物仍按原来的方式自动领取。
- **Crystal Sphere：** 这个事件的小游戏是 `crystal_sphere` 决策，包含 `rows`（`?` 未揭开、`.` 空、字母表示物品）、`hidden_cells`、`items_seen` 和 `divinations_left`。用 `crystal_sphere_click {"x":..,"y":..,"tool":"big"|"small"}` 揭开格子，big 是 3x3 范围。
- **无界面防护：** 游戏逻辑里会直接调用界面对象（屏幕震动、事件房间布局等），在无界面环境下会抛异常，逻辑执行到一半就中断。启动时（每个进程一次，约 1 秒）会把这些调用改写为界面对象不存在时直接跳过。设置 `STS2_NO_UI_GUARD=1` 可以关闭。
- **错误：** `{"type":"error","message":...}`。如果引擎死锁、战斗无法推进，会返回 `"code":"engine_stuck"`（之后一直返回它），不再伪装成判负。遇到时放弃这一局，重新开局。
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
