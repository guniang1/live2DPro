-- Demo 背景图元数据（文件在 MinIO，URL 存此表）
-- 执行：mysql ... < 20260508_background_image.sql

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `background_image` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
  `name` VARCHAR(512) NOT NULL COMMENT '显示名/逻辑名，不含 .jpg 等扩展名',
  `url` VARCHAR(1024) NOT NULL COMMENT 'MinIO 对外访问 URL（与 MINIO_PUBLIC_BASE + 对象键一致）',
  `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='背景图（MinIO 存储，MySQL 仅存索引）';
