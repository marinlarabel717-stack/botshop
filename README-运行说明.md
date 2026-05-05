# Telegram 号铺机器人运行说明（OKPay 完成版）

## 已集成内容

- Telegram 机器人主体：`haopubot.py`
- MongoDB 数据库：`mongo.py`
- OKPay 支付链接充值
- OKPay 回调验签
- 回调成功后自动给用户加 USDT 余额
- 充值订单防重复加款
- `.env` 环境变量配置

## 1. 安装 Python 依赖

在本目录打开终端，执行：

```powershell
python -m pip install -r requirements.txt
```

建议 Python 3.8 / 3.9 / 3.10。

## 2. 准备 MongoDB

默认配置：

```text
地址：mongodb://127.0.0.1:27017/
用户：root
密码：xindi
认证库：admin
业务库：zhidenghao_BOT
```

如果你的 MongoDB 账号密码不同，改 `.env`。

## 3. 配置 `.env`

复制配置文件：

```powershell
copy .env.example .env
```

编辑 `.env`：

```text
BOT_TOKEN=你的TelegramBotToken
OKPAY_SHOP_ID=你的OKPay商户ID
OKPAY_SHOP_TOKEN=你的OKPay商户Token
OKPAY_BOT_USERNAME=你的机器人用户名不带@
OKPAY_CALLBACK_URL=https://你的域名/okpay/callback
OKPAY_CALLBACK_PORT=8088
```

## 4. 配置公网回调

机器人本地会监听：

```text
http://0.0.0.0:8088
```

你需要把公网地址反代到这个端口，例如：

```text
https://你的域名/okpay/callback  ->  http://127.0.0.1:8088
```

如果没有域名，也可以临时用内网穿透，但正式收款建议用服务器域名 + HTTPS。

## 5. 启动机器人

```powershell
.\run.ps1
```

或者双击：

```text
run.bat
```

启动成功后，在 Telegram 对机器人发送：

```text
/start
```

## 6. OKPay 充值流程

用户点击“我要充值”后：

1. 选择固定金额，或输入自定义金额
2. 机器人调用 OKPay `/payLink` 生成支付链接
3. 用户点击按钮打开 OKPay 支付
4. OKPay 回调你的 `OKPAY_CALLBACK_URL`
5. 机器人验签成功后，自动给用户加余额
6. 用户收到“OKPay充值到账”通知

## 7. 管理员

代码里通过数据库 `user.state == '4'` 判断管理员。首次运行后，需要把你的 Telegram user_id 对应用户的 `state` 改成 `'4'`。

## 8. 注意

- `.env` 不要发给别人，里面有 Bot Token 和 OKPay Token。
- OKPay 回调必须公网可访问，否则无法自动到账。
- 已到账订单会标记 `state=1`，防止重复加款。
