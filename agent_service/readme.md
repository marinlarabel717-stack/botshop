# agent_service

这是 `botshop` 代理分销子系统的运行骨架。

当前阶段已具备：

- 独立 `agent_service/.env` 配置入口
- 代理 bot 启动入口 `service.py`
- 首次 `/start` 时自动创建代理用户
- 自动 upsert `agent_bots` 运行记录
- 自动初始化代理专属动态集合索引

## 启动

1. 复制 `.env.example` 为 `.env`
2. 填入 `AGENT_BOT_ID` 与 `AGENT_BOT_TOKEN`
3. 运行：

```bash
py agent_service\service.py
```

## 下一步计划

1. 接代理商品列表与价格覆盖
2. 接代理充值订单与钱包账本
3. 接代理订单、发货、退款
4. 接代理结算与提现

