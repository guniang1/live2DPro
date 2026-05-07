-- 情感交互Live2D数字人系统 — MySQL 8.x 建表脚本
-- 字符集 utf8mb4，引擎 InnoDB

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS `remind_trigger`;
DROP TABLE IF EXISTS `chat_session`;
DROP TABLE IF EXISTS `long_memory`;
DROP TABLE IF EXISTS `user_profile`;
DROP TABLE IF EXISTS `live2d_tts_refer`;
DROP TABLE IF EXISTS `live2d_model_asset`;
DROP TABLE IF EXISTS `system_config`;
DROP TABLE IF EXISTS `persona`;
DROP TABLE IF EXISTS `user`;

SET FOREIGN_KEY_CHECKS = 1;

-- 1. user（用户基础表）
CREATE TABLE `user` (
  `user_id` INT NOT NULL AUTO_INCREMENT COMMENT '用户唯一ID',
  `username` VARCHAR(50) NOT NULL COMMENT '登录用户名',
  `password` VARCHAR(100) NOT NULL COMMENT '加密密码',
  `nickname` VARCHAR(50) DEFAULT NULL COMMENT '用户昵称',
  `phone` VARCHAR(20) DEFAULT NULL COMMENT '手机号',
  `email` VARCHAR(50) DEFAULT NULL COMMENT '邮箱',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `status` TINYINT NOT NULL DEFAULT 1 COMMENT '账号状态(1-正常 0-禁用)',
  PRIMARY KEY (`user_id`),
  UNIQUE KEY `uk_username` (`username`),
  UNIQUE KEY `uk_phone` (`phone`),
  UNIQUE KEY `uk_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户基础表';

-- 2. persona（人设管理表；user_id+package_key 同时非空时表示某用户某模型包专属人设，character_desc 写入 LLM）
CREATE TABLE `persona` (
  `persona_id` INT NOT NULL AUTO_INCREMENT COMMENT '人设ID',
  `character_desc` TEXT NOT NULL COMMENT '性格与角色描述（包级人设时即聊天 system 追加正文）',
  `tone_style` VARCHAR(50) NOT NULL COMMENT '语气风格',
  `default_emotion` VARCHAR(20) DEFAULT NULL COMMENT '默认情绪',
  `user_id` INT DEFAULT NULL COMMENT '绑定用户；与 package_key 均非空时为该包专属人设',
  `package_key` VARCHAR(64) DEFAULT NULL COMMENT '模型包键；与 user_id 均非空时在用户范围内唯一',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `status` TINYINT NOT NULL DEFAULT 1 COMMENT '状态(1-启用 0-禁用)',
  PRIMARY KEY (`persona_id`),
  UNIQUE KEY `uk_persona_user_pkg` (`user_id`, `package_key`),
  KEY `idx_persona_user_id` (`user_id`),
  CONSTRAINT `fk_persona_user_scope` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='人设管理表（含全局模板与按模型包绑定）';

-- 3. system_config（系统配置表）
CREATE TABLE `system_config` (
  `config_id` INT NOT NULL AUTO_INCREMENT COMMENT '配置ID',
  `config_key` VARCHAR(50) NOT NULL COMMENT '配置键(记忆压缩轮数/扫描频率)',
  `config_value` VARCHAR(255) NOT NULL COMMENT '配置值',
  `config_desc` VARCHAR(255) DEFAULT NULL COMMENT '配置描述',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`config_id`),
  UNIQUE KEY `uk_config_key` (`config_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='系统配置表';

-- 4. live2d_model_asset（用户维度：模型包资源索引；动作/表情/模型文件均可入库；public_url 对 OSS/CDN）
CREATE TABLE `live2d_model_asset` (
  `asset_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '资源行ID',
  `user_id` INT NOT NULL COMMENT '关联用户（资源归属；每用户一套模型包）',
  `package_key` VARCHAR(64) NOT NULL COMMENT '资源包目录名/逻辑包名，如 Xiaozi',
  `relative_path` VARCHAR(512) NOT NULL COMMENT '相对 package 的路径，如 motions/待机动画.motion3.json',
  `file_name` VARCHAR(255) NOT NULL COMMENT '文件名',
  `asset_type` VARCHAR(32) NOT NULL COMMENT '类型：model3/motion3/exp3/physics3/cdi3/vtube/json_other',
  `public_url` VARCHAR(768) NOT NULL COMMENT '前端或运行时加载 URL（可与对象存储一致）',
  `object_key` VARCHAR(768) DEFAULT NULL COMMENT '对象存储对象键（MinIO/S3 key）',
  `mime_type` VARCHAR(64) DEFAULT NULL COMMENT 'MIME 类型',
  `logical_name` VARCHAR(128) DEFAULT NULL COMMENT '逻辑名称：如 Expressions[].Name',
  `motion_group` VARCHAR(64) DEFAULT NULL COMMENT '动作组：如 Motions.Idle/TapBody',
  `is_listed_in_model3` TINYINT NOT NULL DEFAULT 0 COMMENT '是否在 model3 引用表中登记',
  `is_entry_model` TINYINT NOT NULL DEFAULT 0 COMMENT '是否主入口 model3.json',
  `file_size` BIGINT DEFAULT NULL COMMENT '文件大小（字节），可选',
  `sort_order` INT NOT NULL DEFAULT 0 COMMENT '同包内排序',
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户 Live2D 模型资源目录（含动作/表情等文件元数据）';

-- 5. live2d_tts_refer（模型包级参考音频绑定：每用户+每模型包一条）
CREATE TABLE `live2d_tts_refer` (
  `refer_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '参考音频绑定ID',
  `user_id` INT NOT NULL COMMENT '关联用户ID',
  `package_key` VARCHAR(64) NOT NULL COMMENT '模型包键，如 Xiaozi',
  `audio_object_key` VARCHAR(768) DEFAULT NULL COMMENT '对象存储键（MinIO/S3 key）',
  `audio_url` VARCHAR(1024) DEFAULT NULL COMMENT '参考音频URL（可为临时链接）',
  `audio_format` VARCHAR(16) DEFAULT NULL COMMENT '音频格式，如 wav/mp3',
  `prompt_text` VARCHAR(512) NOT NULL COMMENT '参考文本（对应 GPT-SoVITS prompt_text）',
  `prompt_language` VARCHAR(32) NOT NULL COMMENT '参考语种（对应 GPT-SoVITS prompt_language）',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`refer_id`),
  UNIQUE KEY `uk_tts_refer_user_pkg` (`user_id`, `package_key`),
  KEY `idx_tts_refer_user` (`user_id`),
  KEY `idx_tts_refer_pkg` (`package_key`),
  CONSTRAINT `fk_tts_refer_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模型包级参考音频绑定（每用户每包唯一）';

-- 6. user_profile（用户画像表）
CREATE TABLE `user_profile` (
  `profile_id` INT NOT NULL AUTO_INCREMENT COMMENT '画像ID',
  `user_id` INT NOT NULL COMMENT '关联用户ID',
  `user_tags` VARCHAR(255) DEFAULT NULL COMMENT '用户标签(考研党/压力大/高数薄弱)',
  `emotion_state` VARCHAR(30) DEFAULT NULL COMMENT '当前情感状态',
  `preferences` TEXT DEFAULT NULL COMMENT '用户偏好',
  `trouble_events` TEXT DEFAULT NULL COMMENT '困扰事件',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`profile_id`),
  UNIQUE KEY `uk_user_id` (`user_id`),
  CONSTRAINT `fk_profile_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户画像表';

-- 7. chat_session（对话存储表）
CREATE TABLE `chat_session` (
  `session_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '对话记录ID',
  `user_id` INT NOT NULL COMMENT '关联用户ID',
  `package_key` VARCHAR(64) NOT NULL COMMENT '模型包键（区分 A/B 模型会话）',
  `user_input` TEXT NOT NULL COMMENT '用户输入内容',
  `ai_reply` TEXT NOT NULL COMMENT 'AI回复内容',
  `emotion_tag` VARCHAR(30) DEFAULT NULL COMMENT '对话情感标签(焦虑/开心/平静)',
  `session_key` VARCHAR(64) NOT NULL COMMENT '会话唯一标识',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '对话时间',
  PRIMARY KEY (`session_id`),
  KEY `idx_user_id` (`user_id`),
  KEY `idx_user_package` (`user_id`, `package_key`),
  KEY `idx_user_package_session` (`user_id`, `package_key`, `session_key`),
  KEY `idx_session_key` (`session_key`),
  CONSTRAINT `fk_chat_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话存储表';

-- 8. long_memory（每用户 + 每模型包一行；长期文本仅 period_overview）
CREATE TABLE `long_memory` (
  `memory_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '记忆ID',
  `user_id` INT NOT NULL COMMENT '关联用户ID',
  `package_key` VARCHAR(64) NOT NULL DEFAULT 'default' COMMENT '模型包键（每角色一行）',
  `memory_type` VARCHAR(20) NOT NULL DEFAULT 'long' COMMENT '记忆类型(长期填 long)',
  `period_overview` TEXT COMMENT '周期对话概要（LLM 从 chat_session 压缩）',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记忆创建时间',
  `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记忆更新时间',
  `last_consolidate_time` DATETIME DEFAULT NULL COMMENT '上次周期概要更新时间',
  PRIMARY KEY (`memory_id`),
  UNIQUE KEY `uk_long_memory_user_pkg` (`user_id`, `package_key`),
  KEY `idx_memory_user` (`user_id`),
  CONSTRAINT `fk_memory_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='核心记忆表';

-- 9. remind_trigger（待办事项/主动关怀核心表）
CREATE TABLE `remind_trigger` (
  `trigger_id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '触发ID',
  `user_id` INT NOT NULL COMMENT '关联用户ID',
  `trigger_type` VARCHAR(30) NOT NULL COMMENT '触发类型(生日/考试/纪念日/日常关怀)',
  `trigger_time` DATETIME NOT NULL COMMENT '触发时间',
  `session_id` BIGINT DEFAULT NULL COMMENT '关联 chat_session：产生该提醒的单轮对话，投递话术据此召回语境',
  `trigger_content` TEXT NOT NULL COMMENT '情景详细描述（触发时结合语境生成话术，非最终台词）',
  `is_triggered` TINYINT NOT NULL DEFAULT 0 COMMENT '是否触发(0-未触发 1-已触发)',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`trigger_id`),
  KEY `idx_trigger_user` (`user_id`),
  KEY `idx_trigger_time` (`trigger_time`, `is_triggered`),
  CONSTRAINT `fk_remind_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_remind_session` FOREIGN KEY (`session_id`) REFERENCES `chat_session` (`session_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='主动关怀触发表';
