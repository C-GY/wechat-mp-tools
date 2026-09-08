# 竞对账号人工标签

`competitor_author_tags` 保存人工给账号添加的标签，一个账号可以对应多个标签。建表脚本位于 [create_competitor_author_tags.sql](../scripts/sql/create_competitor_author_tags.sql)，在 `competitor_monitor` 数据库执行。

| 字段 | 含义 |
| --- | --- |
| author_tag_id | 自增主键 |
| platform | 来源平台，默认 `wechat_channels` |
| author_id | 平台侧作者 ID，对应视频主表的 `author_id` |
| tag_name | 人工添加的标签，最多 128 个字符 |
| created_at | 添加时间，东八区，毫秒精度 |
| updated_at | 最近修改时间，东八区，毫秒精度 |

`(platform, author_id, tag_name)` 唯一，避免同一账号重复添加同名标签。平台、作者 ID 和标签不能为空。账号身份使用平台与作者 ID，账号改名不会丢失关联；作者名通过视频主表查询，不在标签表重复存储。

视频主表中同一作者可以有多条作品，因此不将账号标签外键绑定到某一条视频。也可以在账号尚未采集到作品时先添加标签。账号标签与 `competitor_video_tags` 的视频标签独立，现有采集任务不会增加、修改或删除账号标签。

2026-09-08 已在目标数据库建表。真实 MySQL 集成测试 5 项通过，包含人工多标签、重复及空标签约束、跨账号和平台隔离、账号改名关联，以及采集后人工标签保持不变。测试全部回滚；新表初始为空，原有 985 条视频、1059 条视频标签和 1160 条快照保持不变。

## 页面与批量操作

页面入口为「微信视频号 → 账号标签管理」，也可从竞对监测系统顶部进入。账号名称取最近采集的数据，同一账号的多条视频合并显示；支持名称或作者 ID 搜索、已打/未打标签筛选和指定标签筛选。

勾选账号后点击「批量添加标签」或「批量移除标签」。支持本页全选、跨页勾选和选择全部筛选结果，每次最多 500 个账号、20 个标签。可输入多个标签（逗号、分号或换行分隔），或点击已有标签复用。更改筛选、刷新或保存成功后会清空勾选。行内按钮可以只修改一个账号。

添加仅补充标签，重复提交不重复入库；移除仅删除指定标签。所有选中账号在同一个数据库事务中提交，SQL 失败整体回滚；提交过程中禁止重复点击，失败时保留输入供重试。账号列表包括已采集账号及人工预先打过标签的账号。

接口：`GET /api/competitor_monitor/authors` 返回账号列表；`POST /api/competitor_monitor/author-tags/batch` 接收 `operation`（`add`/`remove`）、`accounts`（明确的 `platform`/`author_id` 对象数组）和 `tags`（字符串数组）。参数先校验，提交前确认全部账号存在，不按账号名称定位。

浏览器到 Flask 再到真实 MySQL 的自动化验证覆盖批量添加、跨页选择、标签过滤、单账号移除、批量移除、失败重试及名称转义；测试使用临时记录并全部回滚。相关用例：`tests/test_competitor_author_tags.py`、`tests/test_competitor_monitor_mysql.py`、`tests/test_competitor_author_tags_ui.py`；后两者需设置 `COMPETITOR_MYSQL_INTEGRATION=1`。

页面交付回归：54 项测试及 8 个子测试通过。在实际页面另对 4 个账号添加临时验证标签，通过独立数据库连接确认提交，重新打开页面确认可读取，再通过批量移除清理全部临时标签。原视频三表记录数保持 985 / 1059 / 1160。详细证据保存在忽略目录 `scratch/competitor-verification/author-tags-page-tests.xml`、`author-tags-live-result.json` 和 `author-tags-page.png`。

## 人工维护 SQL 示例

以下 SQL 也可用于数据库管理工具。示例中的作者 ID 和标签需替换为实际值，写入会话先设置东八区。

```sql
SET time_zone = '+08:00';

-- 查询可供选择的账号；用 ID 确认身份，不按名称关联。
SELECT platform, author_id, author_name, COUNT(*) AS video_count
FROM competitor_videos
WHERE author_id IS NOT NULL AND author_id <> ''
GROUP BY platform, author_id, author_name;

-- 给一个账号添加标签；重复提交同名标签不会新增记录。
INSERT INTO competitor_author_tags (platform, author_id, tag_name)
VALUES ('wechat_channels', '实际作者ID', '重点关注')
ON DUPLICATE KEY UPDATE author_tag_id = author_tag_id;

-- 查询账号的全部人工标签。
SELECT author_tag_id, tag_name, created_at, updated_at
FROM competitor_author_tags
WHERE platform = 'wechat_channels' AND author_id = '实际作者ID'
ORDER BY tag_name;

-- 删除指定账号的一个标签。
DELETE FROM competitor_author_tags
WHERE platform = 'wechat_channels' AND author_id = '实际作者ID'
  AND tag_name = '重点关注';

-- 按账号标签筛选作品。
SELECT v.*
FROM competitor_videos AS v
JOIN competitor_author_tags AS t
  ON t.platform = v.platform AND t.author_id = v.author_id
WHERE t.tag_name = '重点关注'
ORDER BY v.published_at DESC;
```
