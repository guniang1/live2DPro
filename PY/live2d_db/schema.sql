-- Live2D 系统 MySQL 8 建表（utf8mb4 / InnoDB）

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS `remind_trigger`;
DROP TABLE IF EXISTS `background_image`;
DROP TABLE IF EXISTS `chat_session`;
DROP TABLE IF EXISTS `long_memory`;
DROP TABLE IF EXISTS `user_profile`;
DROP TABLE IF EXISTS `live2d_tts_refer`;
DROP TABLE IF EXISTS `live2d_model_asset`;
DROP TABLE IF EXISTS `persona`;
DROP TABLE IF EXISTS `user`;

SET FOREIGN_KEY_CHECKS = 1;

-- 1. user
CREATE TABLE `user` (
  `user_id` INT NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username` VARCHAR(50) NOT NULL COMMENT '登录名',
  `password` VARCHAR(100) NOT NULL COMMENT '密码',
  `nickname` VARCHAR(50) DEFAULT NULL COMMENT '昵称',
  `phone` VARCHAR(20) DEFAULT NULL COMMENT '手机',
  `email` VARCHAR(50) DEFAULT NULL COMMENT '邮箱',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `status` TINYINT NOT NULL DEFAULT 1 COMMENT '状态(1正常0禁用)',
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uk_username` (`username`),
  UNIQUE KEY `uk_phone` (`phone`),
  UNIQUE KEY `uk_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户';

-- 2. persona
CREATE TABLE `persona` (
  `persona_id` INT NOT NULL AUTO_INCREMENT COMMENT '人设ID',
  `character_desc` TEXT NOT NULL COMMENT '角色描述',
  `tone_style` VARCHAR(50) NOT NULL COMMENT '语气',
  `default_emotion` VARCHAR(20) DEFAULT NULL COMMENT '默认情绪',
  `user_id` INT DEFAULT NULL COMMENT '用户ID(与包键同填为包专属)',
  `package_key` VARCHAR(64) DEFAULT NULL COMMENT '模型包键',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `status` TINYINT NOT NULL DEFAULT 1 COMMENT '状态(1启用0禁用)',
  PRIMARY KEY (`persona_id`),
  UNIQUE KEY `uk_persona_user_pkg` (`user_id`, `package_key`),
  KEY `idx_persona_user_id` (`user_id`),
  CONSTRAINT `fk_persona_user_scope` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人设(全局/按包)';

-- 3. live2d_model_asset
CREATE TABLE `live2d_model_asset` (
  `asset_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '资源ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `package_key` VARCHAR(64) NOT NULL COMMENT '模型包键',
  `relative_path` VARCHAR(512) NOT NULL COMMENT '包内相对路径',
  `file_name` VARCHAR(255) NOT NULL COMMENT '文件名',
  `asset_type` VARCHAR(32) NOT NULL COMMENT '类型(model3/motion3/exp3等)',
  `public_url` VARCHAR(768) NOT NULL COMMENT '访问URL',
  `object_key` VARCHAR(768) DEFAULT NULL COMMENT '对象存储键',
  `mime_type` VARCHAR(64) DEFAULT NULL COMMENT 'MIME',
  `logical_name` VARCHAR(128) DEFAULT NULL COMMENT '逻辑名',
  `motion_group` VARCHAR(64) DEFAULT NULL COMMENT '动作组',
  `is_listed_in_model3` TINYINT NOT NULL DEFAULT 0 COMMENT 'model3已登记',
  `is_entry_model` TINYINT NOT NULL DEFAULT 0 COMMENT '入口model3',
  `file_size` BIGINT DEFAULT NULL COMMENT '大小(字节)',
  `sort_order` INT NOT NULL DEFAULT 0 COMMENT '排序',
  `remark` VARCHAR(255) DEFAULT NULL COMMENT '备注',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`asset_id`),
  UNIQUE KEY `uk_user_pkg_rel` (`user_id`, `package_key`, `relative_path`),
  KEY `idx_asset_user` (`user_id`),
  KEY `idx_pkg` (`package_key`),
  KEY `idx_pkg_type` (`package_key`, `asset_type`),
  KEY `idx_pkg_motion_group` (`package_key`, `motion_group`),
  KEY `idx_pkg_listed` (`package_key`, `is_listed_in_model3`),
  CONSTRAINT `fk_model_asset_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Live2D模型资源';

-- 4. live2d_tts_refer
CREATE TABLE `live2d_tts_refer` (
  `refer_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '绑定ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `package_key` VARCHAR(64) NOT NULL COMMENT '模型包键',
  `audio_object_key` VARCHAR(768) DEFAULT NULL COMMENT '音频对象键',
  `audio_url` VARCHAR(1024) DEFAULT NULL COMMENT '音频URL',
  `audio_format` VARCHAR(16) DEFAULT NULL COMMENT '格式(wav/mp3)',
  `prompt_text` VARCHAR(512) NOT NULL COMMENT '参考文本',
  `prompt_language` VARCHAR(32) NOT NULL COMMENT '参考语种',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`refer_id`),
  UNIQUE KEY `uk_tts_refer_user_pkg` (`user_id`, `package_key`),
  KEY `idx_tts_refer_user` (`user_id`),
  KEY `idx_tts_refer_pkg` (`package_key`),
  CONSTRAINT `fk_tts_refer_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='TTS参考音频(每用户每包)';

-- 5. user_profile
CREATE TABLE `user_profile` (
  `profile_id` INT NOT NULL AUTO_INCREMENT COMMENT '画像ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `display_name` VARCHAR(64) DEFAULT NULL COMMENT '称呼',
  `user_tags` VARCHAR(255) DEFAULT NULL COMMENT '标签',
  `emotion_state` VARCHAR(30) DEFAULT NULL COMMENT '情感状态',
  `preferences` TEXT DEFAULT NULL COMMENT '偏好',
  `trouble_events` TEXT DEFAULT NULL COMMENT '困扰',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`profile_id`),
  UNIQUE KEY `uk_user_id` (`user_id`),
  CONSTRAINT `fk_profile_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户画像';

-- 6. chat_session
CREATE TABLE `chat_session` (
  `session_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '记录ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `package_key` VARCHAR(64) NOT NULL COMMENT '模型包键',
  `user_input` TEXT NOT NULL COMMENT '用户输入',
  `ai_reply` TEXT NOT NULL COMMENT 'AI回复',
  `emotion_tag` VARCHAR(30) DEFAULT NULL COMMENT '情感标签',
  `session_key` VARCHAR(64) NOT NULL COMMENT '会话键',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '对话时间',
  PRIMARY KEY (`session_id`),
  KEY `idx_user_id` (`user_id`),
  KEY `idx_user_package` (`user_id`, `package_key`),
  KEY `idx_user_package_session` (`user_id`, `package_key`, `session_key`),
  KEY `idx_session_key` (`session_key`),
  CONSTRAINT `fk_chat_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话记录';

-- 7. long_memory
CREATE TABLE `long_memory` (
  `memory_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '记忆ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `package_key` VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT '模型包键',
  `memory_type` VARCHAR(20) NOT NULL DEFAULT 'long' COMMENT '类型',
  `period_overview` TEXT COMMENT '周期概要',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `last_consolidate_time` DATETIME DEFAULT NULL COMMENT '上次压缩时间',
  PRIMARY KEY (`memory_id`),
  UNIQUE KEY `uk_long_memory_user_pkg` (`user_id`, `package_key`),
  KEY `idx_memory_user` (`user_id`),
  CONSTRAINT `fk_memory_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='长期记忆(每用户每包)';

-- 8. remind_trigger
CREATE TABLE `remind_trigger` (
  `trigger_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '触发ID',
  `user_id` INT NOT NULL COMMENT '用户ID',
  `trigger_type` VARCHAR(30) NOT NULL COMMENT '类型',
  `trigger_time` DATETIME NOT NULL COMMENT '触发时间',
  `session_id` BIGINT DEFAULT NULL COMMENT '来源会话',
  `trigger_content` TEXT NOT NULL COMMENT '情景概要',
  `is_triggered` TINYINT NOT NULL DEFAULT 0 COMMENT '投递(0待投1已投)',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`trigger_id`),
  KEY `idx_trigger_user` (`user_id`),
  KEY `idx_trigger_time` (`trigger_time`, `is_triggered`),
  CONSTRAINT `fk_remind_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_remind_session` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`session_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='主动关怀触发';

-- 9. background_image
CREATE TABLE `background_image` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
  `name` VARCHAR(512) NOT NULL COMMENT '逻辑名(无扩展名)',
  `url` VARCHAR(1024) NOT NULL COMMENT '访问URL',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='背景图索引';
