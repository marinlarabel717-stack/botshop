# 动态 Emoji / 自定义 Emoji 使用说明

本版本已支持 Telegram 自定义 Emoji 标记语法。

## 写法

在后台文案或按钮名称里写：

```text
[emoji:自定义emoji_id:兜底emoji]文字
```

也支持别名：

```text
[ce:自定义emoji_id:兜底emoji]文字
[custom_emoji:自定义emoji_id:兜底emoji]文字
```

示例：

```text
[emoji:5368324170671202286:📱]手机账号
[emoji:5368324170671202287:🫥]匿名专区
```

## 用在哪里

### 1. 消息文案

欢迎语、图文内容、OKPay 充值提示等，只要由机器人 `send_message/send_photo/sendAnimation/sendVideo` 发出，都会自动识别：

```text
欢迎来到 [emoji:5368324170671202286:📱] 号铺
```

程序会自动转换成 Telegram 的 `<tg-emoji emoji-id="...">📱</tg-emoji>` 格式发送。

### 2. 底部菜单按钮

后台「菜单按钮」里，按钮名称可以写：

```text
[emoji:5368324170671202286:📱]商品列表
```

如果当前库/账号支持按钮图标，会显示动态 emoji 图标；不支持时，会自动退化成普通兜底 emoji：

```text
📱商品列表
```

### 3. 内联按钮 / 尾随按钮

尾随按钮仍按原格式：

```text
[emoji:5368324170671202286:📱]客服&https://t.me/xxx | [emoji:5368324170671202287:🫥]频道&https://t.me/yyy
```

## 注意事项

- 自定义 emoji 必须有真实的 `custom_emoji_id`。
- Telegram 对按钮动态图标有限制：Bot API 要求机器人满足条件，例如机器人拥有者 Premium 或机器人购买过 Fragment 额外用户名。
- 如果按钮动态图标不生效，程序会自动显示兜底普通 emoji，不影响使用。
- 消息文案里的自定义 emoji 通常比按钮图标更稳定。

## 如何拿 custom_emoji_id

最简单方式：后续可以加一个 `/emojiid` 管理命令，让管理员把动态 emoji 发给机器人，机器人返回它的 `custom_emoji_id`。
当前版本先支持手动填 ID。
