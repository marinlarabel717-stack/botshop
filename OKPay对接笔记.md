# OKPay API 对接笔记

来源：用户提供的 `okpay.zip`，包含 `Okpay.py`、`OkayPay.php`、`Okpay.txt`。

## 基础信息

- API 根地址：`https://api.okaypay.me/shop/`
- 请求方式：POST
- 商户参数：
  - `shop_id` / 文档中的 `id`
  - `shop_token`
- 支持币种：`USDT`、`TRX`

## 签名规则

1. 请求参数加入 `id=shop_id`
2. 过滤空值
3. 按 key 升序排序
4. 拼接查询字符串
5. 后面追加 `&token=shop_token`
6. MD5 后转大写
7. 放入 `sign` 字段

伪代码：

```python
params['id'] = shop_id
params = remove_empty(params)
params = sort_by_key(params)
query = urlencode(params)
sign = md5(unquote(query) + '&token=' + shop_token).upper()
params['sign'] = sign
```

## 主要接口

### 1. 创建支付链接 `/payLink`

用途：用户充值时生成 OKPay 支付链接。

参数：

- `unique_id`：本系统订单号，建议必填，防重复
- `name`：显示名称
- `amount`：金额
- `return_url`：支付完成后返回地址，可以填 Telegram bot 链接
- `coin`：`USDT` 或 `TRX`
- `callback_url`：可选，单笔订单回调地址

返回：

- `data.order_id`：OKPay 订单号
- `data.pay_url`：支付链接

### 2. 提现/转账 `/transfer`

用途：从商户余额转账给指定 Telegram 用户。

参数：

- `unique_id`：本系统订单号
- `name`：显示名称
- `amount`：金额
- `to_user_id`：收款用户 Telegram ID，用户需启动过 OKPay 钱包
- `coin`：`USDT` 或 `TRX`
- `callback_url`：提现回调地址

返回：

- `data.order_id`

### 3. 查询提现 `/checkTransfer`

参数：

- `unique_id`：本系统提现订单号

返回：

- `status`：`0` 未成功，`1` 成功，`2` 失败
- `amount`
- `coin`
- `to_user_id`

### 4. 查询充值 `/checkDeposit`

参数：

- `unique_id`：本系统充值订单号

返回：

- `status`：`0` 未付款，`1` 已付款
- `amount`

### 5. 查询商户余额 `/balance`

返回：

- `usdt`
- `trx`
- `cny`

### 6. 检查 TG 用户 `/censorUserByTG`

参数：

- `telegramID`

返回：

- `exist`：true / false

## 回调格式

充值和提现都会回调。

主要字段：

- `id`：商户 ID
- `sign`：签名
- `status`：通常是 `success`
- `code`：通常是 `200`
- `data[order_id]`：OKPay 订单号
- `data[unique_id]`：本系统订单号
- `data[pay_user_id]`：支付/收款用户 TG ID
- `data[amount]`：金额
- `data[coin]`：币种
- `data[status]`：
  - 充值：`0` 未支付，`1` 已支付
  - 提现：`0` 等待，`1` 成功，`2` 失败
- `data[type]`：`deposit` 或 `withdraw`

## 对接到当前 Telegram 商城机器人的建议流程

### 充值流程

1. 用户点击“我要充值”或输入充值金额
2. 生成本地订单号 `unique_id`
3. 写入 MongoDB `topup` 表，状态设为未支付
4. 调用 OKPay `/payLink`
5. 把 `pay_url` 发给用户
6. 新增一个公网 HTTP 回调服务 `/okpay/callback`
7. 回调收到后：
   - 校验签名
   - 检查 `data.type == deposit`
   - 检查 `data.status == 1`
   - 用 `unique_id` 查本地订单
   - 防重复处理：已到账订单不能再次加余额
   - 给用户 `USDT` 余额加款
   - 更新订单状态

### 提现流程（如果需要）

1. 用户提交提现金额和目标 Telegram ID
2. 检查用户余额
3. 扣减或冻结余额
4. 调用 OKPay `/transfer`
5. 等待回调或轮询 `/checkTransfer`
6. 成功则完成订单，失败则退回余额

## 需要老大提供的参数

后续真正对接时需要：

- OKPay `shop_id`
- OKPay `shop_token`
- 回调域名 / 公网服务器地址
- 是否只做充值，还是充值+提现都做
- 充值按钮是否保留原来的 TRC20 小数匹配方案，还是完全替换为 OKPay
