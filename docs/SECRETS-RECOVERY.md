# 密钥与配置的恢复路径

`.env` 是本机唯一一份真实密钥，**永不入库**（`.gitignore` 的 `.env` 一行）。
丢了它会怎样、怎么恢复，写在这里，免得下次换机器靠回忆。

## 丢了 `.env` 会丢什么

| 内容 | 丢了之后 |
|---|---|
| `YIXIANG_API_KEY` | 所有真实模型调用不可用；需要重新申请密钥 |
| `YIXIANG_MAIN_MODEL` 等模型配置 | 回落到默认值（`deepseek-flash`），行为与之前不同 |
| `YIXIANG_BUDGET_CNY_PER_DAY` 等预算 | 回落到 `0.5`，`ops cost` 的告警线随之变化 |
| `YIXIANG_BANGUMI_TOKEN` | 读不了自己的 Bangumi 收藏（"按我的口味推荐"这一条能力消失）；搜番 / 查评分不受影响，重新生成即可 |

`data/usage.jsonl` 里历史行的 `cost_cny` **不会被重算**（成本是当时的事实），
所以密钥丢了不会让账本变成错的，只会让"下一轮"接不上。

## 恢复路径（按优先级）

1. **本机副本**：`data/backups/secrets/.env.YYYY-MM-DD`（`python -m yixiang backup` 或手动生成）。
   直接复制回仓库根目录的 `.env` 即可。
2. **密码管理器 / 系统钥匙串**：把 `.env` 全文存进 1Password / Bitwarden / Windows
   凭据管理器的"安全笔记"，别存成明文文件同步到网盘。
3. **重新生成**：`.env.example` 是字段清单，照着填一遍；只有 `YIXIANG_API_KEY`
   需要去供应商后台重新申请。

## 为什么副本落在 `data/` 里

`data/` 已经整体 gitignore（既包括仓库根的 `.gitignore`，也包括 `data/` 私有仓自己的
`PRIVATE_GITIGNORE`：`backups/` 在其中）。把副本放这儿是唯一同时满足
"密钥永远不进 git"和"密钥必须可恢复"两条要求的位置——不新增一个要记得忽略的路径。

副本一天一份（同名覆盖）：多留几份密钥副本只会扩大泄露面。

## 纪律

- `.env`、`data/backups/secrets/` 都**不进 git、不进聊天记录、不进截图**；
- trace / 日志 / 接口只出掩码（`web/console.py::mask_secret`）；
- 恢复之后跑一遍 `python -m yixiang doctor`，第 7 项「密钥可恢复性」要转成 OK。
