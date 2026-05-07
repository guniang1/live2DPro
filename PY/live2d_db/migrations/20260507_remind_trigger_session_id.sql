-- remind_trigger：增加 session_id，绑定产生提醒的单轮 chat_session
-- 已有库执行前请备份。若列已存在则跳过本脚本。

ALTER TABLE remind_trigger
  ADD COLUMN session_id BIGINT NULL COMMENT '关联 chat_session：产生该提醒的单轮对话' AFTER trigger_time;

ALTER TABLE remind_trigger
  ADD CONSTRAINT fk_remind_session FOREIGN KEY (session_id) REFERENCES chat_session (session_id)
  ON DELETE SET NULL ON UPDATE CASCADE;
