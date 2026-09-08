-- 在 competitor_monitor 数据库执行；账号标签仅由人工维护。
-- 写入会话应执行 SET time_zone = '+08:00'，与视频三表保持一致。
CREATE TABLE IF NOT EXISTS `competitor_author_tags` (
  `author_tag_id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '账号标签记录主键',
  `platform` varchar(32) NOT NULL DEFAULT 'wechat_channels' COMMENT '来源平台',
  `author_id` varchar(128) NOT NULL COMMENT '平台侧作者ID，与视频主表author_id对应',
  `tag_name` varchar(128) NOT NULL COMMENT '人工添加的账号标签',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '标签添加时间（东八区）',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
    ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '标签修改时间（东八区）',
  PRIMARY KEY (`author_tag_id`),
  UNIQUE KEY `uq_competitor_author_tag` (`platform`, `author_id`, `tag_name`),
  KEY `ix_competitor_author_tag_name` (`tag_name`, `platform`, `author_id`),
  CONSTRAINT `ck_competitor_author_tag_platform` CHECK (CHAR_LENGTH(TRIM(`platform`)) > 0),
  CONSTRAINT `ck_competitor_author_tag_author` CHECK (CHAR_LENGTH(TRIM(`author_id`)) > 0),
  CONSTRAINT `ck_competitor_author_tag_name` CHECK (CHAR_LENGTH(TRIM(`tag_name`)) > 0)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_0900_ai_ci
COMMENT='竞对账号人工标签表，按平台和作者ID关联账号，不由采集覆盖';
