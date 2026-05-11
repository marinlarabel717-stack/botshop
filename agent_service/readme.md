# agent_service

这是 `botshop` 代理分销子系统的运行骨架。

当前阶段已具备：

- 独立 `agent_service/.env` 配置入口
- 代理 bot 启动入口 `service.py`
- 首次 `/start` 时自动创建代理用户
- 自动 upsert `agent_bots` 运行记录
- 自动初始化代理专属动态集合索引
- 代理商品分类目录 / 商品列表展示
- 代理价格覆盖读取
- 管理员命令设置/清理价格覆盖
- 代理 TRC20 充值订单创建
- 代理钱包账本入账 helper
- 管理员手动上分 / 手动确认到账命令

## 启动

1. 复制 `.env.example` 为 `.env`
2. 填入 `AGENT_BOT_ID` 与 `AGENT_BOT_TOKEN`
3. 运行：

```bash
py agent_service\service.py
```

## 管理员命令

```bash
/agent_price <nowuid> <price> [display_name]
/agent_price_clear <nowuid>
/agent_credit <user_id> <amount> [note]
/agent_mark_paid <order_id> [txid]
```

## 下一步计划

1. 把真实链上监听 / 主支付系统接到代理充值订单
2. 接代理订单、发货、退款
3. 接代理结算与提现
4. 接代理 bot 与主系统 clone/systemd 流程
