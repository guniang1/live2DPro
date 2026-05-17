-- user_profile：独立称呼字段，供 24h 画像总结与主对话注入
ALTER TABLE `user_profile`
  ADD COLUMN `display_name` VARCHAR(64) DEFAULT NULL COMMENT '用户自称/常用称呼' AFTER `user_id`;
