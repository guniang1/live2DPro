# 聊天记忆分层（瞬时 / 短期 / 长期）

本文说明 WebSocket 对话链路中 **瞬时记忆**、**短期记忆** 在 Redis 中的形态、写入顺序，以及如何拼装进大模型上下文；并简述 **长期记忆**（MySQL + Redis）与后台固化任务。实现以 `PY/live2d_db/memory_layers.py`、`PY/router/wschat.py`、`PY/live2d_db/long_memory_consolidator.py` 为准。

**说明：** 历史上曾使用独立的 Redis List `mem:acc:*` 作为「摘要累积队列」，已在代码中移除；瞬时与短期之间不再经由该键中转。

---

## 1. 分层含义

| 层级 | 用途 | 给主对话模型的方式 |
|------|------|---------------------|
| **瞬时记忆** | 最近若干轮 **完整** 用户/助手文本 | 进入 `messages`，按时间顺序的 `user` / `assistant` 消息 |
| **短期记忆** | 更早内容的 **压缩**：规则精简条（`type: rule`）；结构上也可存放摘要条（`type: summary`） | 拼入 **同一条** `system` 正文中的「短期记忆」段落 |

瞬时列表每次有新对话会向列表头部压入一轮，超出窗口则从尾部挤出；挤出轮经规则精简后写入 **短期** 列表（见下文）。

当前 **`wschat.py` 每轮仅调用** `append_instant_evict_to_short`**，不会在服务端自动按 N 轮触发 LLM 摘要。** 若业务需要摘要条，可自行调用摘要模型后通过 `memory_layers.push_summary_entry` 写入短期 List（见第 4 节）。

---

## 2. Redis 键与类型

二者均为 **Redis List**，元素为 **JSON 字符串**。键均包含 **规范化后的** `package_key`（与对象存储路径、MySQL 一致，见 `package_key_util`）。

| 角色 | 键模式（默认前缀） | 示例 |
|------|-------------------|------|
| 瞬时 | `{REDIS_INSTANT_LIST_PREFIX}:{user_id}:{pkg}`，前缀默认 `moment` | `moment:1:default` |
| 短期 | `{REDIS_SHORT_TERM_PREFIX}:{user_id}:{pkg}`，前缀默认 `short` | `short:1:default` |

---

## 3. List 元素 JSON 形态

### 3.1 瞬时（每元素一轮）

字段缩写，内容为裁切后的原文与时间戳：

```json
{"u": "用户发言…", "a": "助手回复…", "ts": "2026-05-05T08:38:40.123456+00:00"}
```

### 3.2 短期 · `type: "rule"`

由挤出瞬时窗口的对话轮 **规则精简** 得到（可能缩短用户句、助手句；极短敷衍回复可能清空 `ai_response`）：

```json
{
  "type": "rule",
  "time": "2026-05-05T08:38:40.123456+00:00",
  "user_question": "…",
  "ai_response": "…"
}
```

### 3.3 短期 · `type: "summary"`

结构预留：由业务侧生成摘要文本后调用 **`push_summary_entry`** 写入；入库前会做长度上限裁切（当前实现约 2000 字上限）：

```json
{
  "type": "summary",
  "time": "2026-05-05T08:40:00.123456+00:00",
  "text": "…摘要正文…"
}
```

---

## 4. 每轮结束后的写入（当前主干）

在 `_append_turn_memory_layers`（`wschat.py`）中：

1. **`append_instant_evict_to_short`**：`LPUSH` 本轮进瞬时列表，`LTRIM` 保留最近 `INSTANT_MEMORY_MAX_TURNS` 轮；被挤出的最旧轮解析后 **`rule_compact_turn`**，再 **`LPUSH`** 进短期列表。

### 4.1 `push_summary_entry` 与 rule 去重（扩展用）

`memory_layers.push_summary_entry` 可将 `type: summary` 压入短期列表头部。若传入本轮摘要所覆盖的若干轮的 **`time` 集合**（`prune_rule_turn_times`），会从短期列表中 **移除** `type=="rule"` 且 **`time` 落在该集合内** 的条目，避免摘要与同一段对话的 rule 重复占用上下文。

### 4.2 摘要提示维度（扩展参考）

当前 WebSocket 链路 **未内置** 周期性调用摘要模型。若在其它模块中接入摘要生成，可将下列维度写入提示词（无信息可写「无」或省略），且禁止编造：

1. **主题与诉求**：在聊什么、想解决或弄清什么  
2. **关键事实**：称呼/姓名、身份关系、地点、偏好、数字、约定等用户交代的事实  
3. **情绪与态度**：明显情绪、顾虑、人际语境（若有）  
4. **助手要点**：助手已给出的核心建议、结论、步骤或承诺  
5. **待跟进**：悬而未决、尚未答复的问题（若有）  

生成完成后调用 **`push_summary_entry`** 写入短期 List 即可参与 `_build_memory_for_model` 的拼装。

---

## 5. 长期记忆（MySQL `period_overview` + Redis 字符串）

- **数据源约定**：固化输入 **只从 MySQL 表 `chat_session`** 按时间窗读出（`ChatSessionRepository.list_for_long_memory_window`）；**不**用 Redis 瞬时/短期列表作为来源。
- **一行**对应 `(user_id, package_key)`。当前产品约定：LLM 固化 **只维护** 列 **`period_overview`（周期概要）**；新摘要可 **追加** 到已有概要之后（分隔线拼接）。注入对话 **system** 时仅使用该维度，见 [`PY/live2d_db/long_memory_fields.py`](PY/live2d_db/long_memory_fields.py) 中 **`LONG_MEMORY_DIMENSIONS`**（现为单一「周期概要」）。
- **Redis** `long:{user_id}:{pkg}` 存 **`merge_long_memory_record_for_prompt`** 生成的合并正文（带 `【周期概要】` 标题块）。
- **后台 `long_memory_consolidator.py`**（由 **`PY/main.py`** 的 **`lifespan`** 调用 **`start_long_memory_consolidator`**）：将窗口内会话 **规则合并为一段口述式叙述** → **Ollama** 生成 **`period_overview`**（可做严格重试、扩写、`finalize` 修补等，见源码）→ **`upsert_by_user_pkg`** 写库并 **`write_long_memory_text`** 刷新 Redis。**手动回填**：`python -m live2d_db.long_memory_consolidator`（加载 **`PY/.env`**）。
- **调度与时间窗**：默认 **7 天**内会话、同一用户同一包 **最短 24 小时**再次固化、后台约 **每 24 小时**扫描——常量 **`_SOURCE_WINDOW_SEC`** / **`_MIN_GAP_SEC`** / **`_INTERVAL_SEC`**；模型与地址仍共用 **`OLLAMA_MODEL`**、**`OLLAMA_HOST`**。
- 已有库若缺列：执行迁移（如 **`PY/live2d_db/migrations/alter_long_memory_period_overview.sql`**，以实际环境为准）。

---

## 6. 主对话模型端如何看到短期内容

- 从短期列表 **`LRANGE` 后解析**，按「从新到旧」格式化成一段纯文本（摘要行前可加 `[摘要 …]`，rule 行则为时间与「用户：」「助手：」拼接）。
- 若有内容，会附加在包级 system 之后，标题沿用「短期记忆」相关提示（具体措辞见 `_build_memory_for_model`）。

瞬时轮次仍单独占 `messages` 中的多轮 `user`/`assistant`，与 system 中的短期块叠加。

---

## 7. 相关环境变量一览

| 变量 | 含义（默认见代码） |
|------|---------------------|
| `REDIS_INSTANT_LIST_PREFIX` | 瞬时 List 键前缀，默认 `moment` |
| `REDIS_SHORT_TERM_PREFIX` | 短期 List 键前缀，默认 `short` |
| `INSTANT_MEMORY_MAX_TURNS` | 瞬时保留轮数，默认 `5` |
| `INSTANT_MEMORY_IDLE_TTL_SECONDS` | 瞬时键空闲 TTL（秒），默认 `3600` |
| `SHORT_TERM_TTL_SECONDS` | 短期 List TTL（秒），默认 `86400` |
| `SHORT_TERM_MAX_ENTRIES` | 短期 List 最多条数，默认 `20` |
| `SHORT_TERM_PROMPT_MAX_CHARS` | 拼进 system 的短期块字符上限，默认 `4000` |
| `OLLAMA_MODEL` / `OLLAMA_HOST` | 长期固化与聊天共用（见 `long_memory_consolidator.py`） |

长期固化扫描间隔、会话窗、规则压缩参数见 **`PY/live2d_db/long_memory_consolidator.py`** 顶部常量，不在此逐条列举环境变量。

Redis 连接：`REDIS_URL` / `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD`（见 `redis_factory.py`）。

---

## 8. 迁移与运维提示

- 修改瞬时键前缀（例如由旧版 `session` 改为 `moment`）后，历史键不会自动改名；若需沿用旧数据可对 Redis 使用 `RENAME`，或在 `.env` 中暂时保留旧前缀。
- 清空某用户某包的记忆：`memory_layers.delete_memory_keys` 会删除 **瞬时、短期** List 键及 **长期** Redis STRING。
- 登录预热向 Redis 灌历史：`http_api` 侧调用 `seed_from_mysql_rows` —— 仅重建 **瞬时窗口 + 更早轮的 rule**（写入短期），不涉及已移除的 `mem:acc`。
- 线上若仍存在历史键 `mem:acc:{user_id}:{pkg}`，可择机手动 `DEL` 回收空间（代码已不再读写）。

---

## 9. 源码索引

| 模块 | 路径 |
|------|------|
| 键名、rule/summary、读写与瞬时挤出写入短期 | `PY/live2d_db/memory_layers.py` |
| 拼装 messages/system、每轮写入瞬时/短期、长期块读取 | `PY/router/wschat.py` |
| 长期固化后台与手动回填 | `PY/live2d_db/long_memory_consolidator.py` |
