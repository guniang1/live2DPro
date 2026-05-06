-- 长期记忆仅保留 period_overview；删除已不再使用的多维度 TEXT 列。
-- 执行前请备份数据库。MySQL 8.0+ / InnoDB。
-- 在目标库下执行（或先 USE `库名`;）。

ALTER TABLE `long_memory`
  DROP COLUMN `identity_addressing`,
  DROP COLUMN `goals_and_concerns`,
  DROP COLUMN `persona_relation_preference`,
  DROP COLUMN `corrections_and_taboos`,
  DROP COLUMN `social_circle_mentions`,
  DROP COLUMN `spatiotemporal_rhythm`,
  DROP COLUMN `commitments_and_followups`,
  DROP COLUMN `time_sensitive_events`,
  DROP COLUMN `source_confidence_notes`;
