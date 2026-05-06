# 情感交互Live2D数字人系统 数据库表完整字段设计

本文档按业务语义罗列主要表字段，便于论文与评审对照。**实现上的权威来源为 `PY/live2d_db/schema.sql`**（当前另有 **`live2d_model_asset`**、**`live2d_tts_refer`** 等对象存储配套表，下文若未单独成章请直接查脚本）。早期「八张核心表」提法与现网 schema 可能不完全一一对应。

---

## 1. user（用户基础表）

**核心作用**：存储用户账号、基础信息

|字段名|数据类型|约束|注释|
|---|---|---|---|
|user_id|INT|PRIMARY KEY AUTO_INCREMENT|用户唯一ID|
|username|VARCHAR(50)|NOT NULL UNIQUE|登录用户名|
|password|VARCHAR(100)|NOT NULL|加密密码|
|nickname|VARCHAR(50)|NULL|用户昵称|
|phone|VARCHAR(20)|NULL UNIQUE|手机号|
|email|VARCHAR(50)|NULL UNIQUE|邮箱|
|create_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP|创建时间|
|update_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP|更新时间|
|status|TINYINT|NOT NULL DEFAULT 1|账号状态(1-正常 0-禁用)|
---

## 2. chat_session（对话存储表）

**核心作用**：存储原始对话记录（长期记忆原料）

|字段名|数据类型|约束|注释|
|---|---|---|---|
|session_id|BIGINT|PRIMARY KEY AUTO_INCREMENT|对话记录ID|
|user_id|INT|NOT NULL FOREIGN KEY|关联用户ID|
|package_key|VARCHAR(64)|NOT NULL|模型包键（区分 A/B 模型会话）|
|user_input|TEXT|NOT NULL|用户输入内容|
|ai_reply|TEXT|NOT NULL|AI回复内容|
|emotion_tag|VARCHAR(30)|NULL|对话情感标签(焦虑/开心/平静)|
|session_key|VARCHAR(64)|NOT NULL|会话唯一标识|
|create_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP|对话时间|

**索引建议**：`(user_id, package_key)`、`(user_id, package_key, session_key)`，用于同用户多模型与会话窗口隔离检索。
---

## 3. long_memory（核心记忆表）

**核心作用**：按 **`(user_id, package_key)` 唯一一行**，固化「周期概要」；瞬时/短期对话仍在 **Redis**（见 `docs/聊天双层记忆与Redis摘要.md`）。**权威定义见 `PY/live2d_db/schema.sql`。**

| 字段名 | 数据类型 | 约束 | 注释 |
|---|---|---|---|
| memory_id | BIGINT | PRIMARY KEY AUTO_INCREMENT | 记忆 ID |
| user_id | INT | NOT NULL，FK → user | 用户 |
| package_key | VARCHAR(64) | NOT NULL，默认 `default` | 模型包键 |
| memory_type | VARCHAR(20) | NOT NULL，默认 `long` | 类型（当前实践填 `long`） |
| period_overview | TEXT | NULL | 周期对话概要（由 `long_memory_consolidator` 从 `chat_session` 压缩写入，可多次追加） |
| create_time | DATETIME | NOT NULL DEFAULT CURRENT_TIMESTAMP | 创建时间 |
| update_time | DATETIME | NOT NULL ON UPDATE CURRENT_TIMESTAMP | 更新时间 |
| last_consolidate_time | DATETIME | NULL | 上次长期固化时间 |

**索引**：`UNIQUE (user_id, package_key)`；`KEY idx_memory_user (user_id)`。

---

## 4. persona（人设管理表）

**核心作用**：存储虚拟数字人基础人设；支持 **全局模板** 与 **按用户+模型包绑定** 两类用法（详见 `docs/文档.md`）。

|字段名|数据类型|约束|注释|
|---|---|---|---|
|persona_id|INT|PRIMARY KEY AUTO_INCREMENT|人设ID|
|character_desc|TEXT|NOT NULL|性格/角色描述；包级行亦作为聊天 system 与 MiMo【角色】|
|tone_style|VARCHAR(50)|NOT NULL|语气风格；包级行必填写入，用于 MiMo【指导】|
|default_emotion|VARCHAR(20)|NULL|默认情绪|
|user_id|INT|NULL，FK → user|绑定用户；与 package_key 均非空时为该用户某模型包专属人设|
|package_key|VARCHAR(64)|NULL|模型包键；与 user_id 均非空时 (`user_id`,`package_key`) 唯一|
|create_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP|创建时间|
|status|TINYINT|NOT NULL DEFAULT 1|状态(1-启用 0-禁用)|

**索引与约束**：`UNIQUE (user_id, package_key)`（MySQL 允许多行 `(NULL,NULL)` 全局模板）；`FOREIGN KEY (user_id)` 引用 `user`。

---

## 5. user_profile（用户画像表）

**核心作用**：存储用户标签、情感状态、偏好

|字段名|数据类型|约束|注释|
|---|---|---|---|
|profile_id|INT|PRIMARY KEY AUTO_INCREMENT|画像ID|
|user_id|INT|NOT NULL FOREIGN KEY UNIQUE|关联用户ID|
|user_tags|VARCHAR(255)|NULL|用户标签(考研党/压力大/高数薄弱)|
|emotion_state|VARCHAR(30)|NULL|当前情感状态|
|preferences|TEXT|NULL|用户偏好|
|trouble_events|TEXT|NULL|困扰事件|
|update_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP|更新时间|
---

## 6. remind_trigger（待办事项/主动关怀核心表）

**核心作用**：主动关怀触发索引，定时任务扫描

|字段名|数据类型|约束|注释|
|---|---|---|---|
|trigger_id|BIGINT|PRIMARY KEY AUTO_INCREMENT|触发ID|
|user_id|INT|NOT NULL FOREIGN KEY|关联用户ID|
|trigger_type|VARCHAR(30)|NOT NULL|触发类型(生日/考试/纪念日/日常关怀)|
|trigger_time|DATETIME|NOT NULL|触发时间|
|memory_id|BIGINT|NULL FOREIGN KEY|关联记忆ID|
|trigger_content|TEXT|NOT NULL|关怀文案内容|
|is_triggered|TINYINT|NOT NULL DEFAULT 0|是否触发(0-未触发 1-已触发)|
|create_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP|创建时间|
---

## 7. live2d_action（历史设计说明）

当前仓库 **`PY/live2d_db/schema.sql`** **未包含** `live2d_action` 表；表情与动作由 **`/ws/chat`** 侧 **动作 LLM** 结合 Resources **`catalog`** 在运行时选取，**不再**通过该表做静态映射。若旧库仍残留此表，可与业务确认后迁移或废弃；**`/api/live2d-actions`** 路由已在当前 `http_api.py` 中移除。

---

## 8. system_config（系统配置表）

**核心作用**：存储系统参数，避免硬编码

|字段名|数据类型|约束|注释|
|---|---|---|---|
|config_id|INT|PRIMARY KEY AUTO_INCREMENT|配置ID|
|config_key|VARCHAR(50)|NOT NULL UNIQUE|配置键(记忆压缩轮数/扫描频率)|
|config_value|VARCHAR(255)|NOT NULL|配置值|
|config_desc|VARCHAR(255)|NULL|配置描述|
|update_time|DATETIME|NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP|更新时间|
---

**建表脚本**：以 **`PY/live2d_db/schema.sql`** 为准；增量变更见同目录 **`migrations/`**。