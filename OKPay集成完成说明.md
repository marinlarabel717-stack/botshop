# OKPay 集成完成说明

## 改动内容

已在 `haopubot.py` 中集成 OKPay：

1. 新增 OKPay 配置读取：
   - `OKPAY_API_URL`
   - `OKPAY_SHOP_ID`
   - `OKPAY_SHOP_TOKEN`
   - `OKPAY_NAME`
   - `OKPAY_BOT_USERNAME`
   - `OKPAY_CALLBACK_URL`
   - `OKPAY_CALLBACK_HOST`
   - `OKPAY_CALLBACK_PORT`

2. 新增 OKPay 支付函数：
   - 创建支付链接 `/payLink`
   - OKPay 请求签名
   - OKPay 回调验签

3. 修改充值逻辑：
   - 固定金额充值按钮现在会创建 OKPay 支付链接
   - 自定义充值金额也会创建 OKPay 支付链接
   - 不再要求用户按随机小数金额转账

4. 新增回调服务：
   - 机器人启动时会监听 `OKPAY_CALLBACK_HOST:OKPAY_CALLBACK_PORT`
   - 默认监听：`0.0.0.0:8088`
   - OKPay 回调成功后自动给用户加 USDT 余额
   - 已到账订单标记 `state=1`，防止重复加款

## 需要配置

复制 `.env.example` 为 `.env`，然后填写：

```text
BOT_TOKEN=你的TelegramBotToken
OKPAY_SHOP_ID=你的OKPay商户ID
OKPAY_SHOP_TOKEN=你的OKPay商户Token
OKPAY_BOT_USERNAME=你的机器人用户名不带@
OKPAY_CALLBACK_URL=https://你的域名/okpay/callback
OKPAY_CALLBACK_PORT=8088
```

## 回调反代

如果机器人跑在服务器上，建议用 Nginx/宝塔反代：

```text
https://你的域名/okpay/callback -> http://127.0.0.1:8088
```

OKPay 必须能访问这个公网 HTTPS 地址，否则无法自动到账。

## 启动

```powershell
python -m pip install -r requirements.txt
.\run.ps1
```

## 测试流程

1. 启动机器人
2. Telegram 发送 `/start`
3. 点击“我要充值”
4. 选择金额或自定义金额
5. 机器人返回 OKPay 支付按钮
6. 支付成功后等待 OKPay 回调
7. 用户余额自动增加，并收到到账通知

## 注意

- `.env` 里有敏感信息，不要发给别人。
- 当前机器没有真正 Python 环境，无法在本机完成 py_compile 测试；代码已按现有逻辑完成集成。
- 如果 OKPay 实际回调字段和文档不同，需要根据真实回调样例微调验签字段顺序。
