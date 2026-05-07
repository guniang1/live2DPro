-- 移除 remind_trigger.memory_id：语境仅通过 session_id → chat_session。
-- 执行前备份。若外键名不是 fk_remind_memory，请先查 INFORMATION_SCHEMA.TABLE_CONSTRAINTS 后改下列第一句。

ALTER TABLE remind_trigger DROP FOREIGN KEY fk_remind_memory;

ALTER TABLE remind_trigger DROP COLUMN memory_id;
