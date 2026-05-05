# botshop 仓库工作流

- 仓库地址：`https://github.com/marinlarabel717-stack/botshop.git`
- 克隆命令：`git clone https://github.com/marinlarabel717-stack/botshop.git`

## 自动规则

这个仓库已经配置了 `core.hooksPath=.githooks`：

1. **每次 commit 前** 自动把 `VERSION` 的补丁号 `+1`
2. **每次 commit 后** 自动 `git push origin 当前分支`

## 日常更新

```bash
git add .
git commit -m "你的修改说明"
```

执行完后会自动推送。

## 注意

- 只有 **commit** 才会触发自动版本号和自动推送
- 单纯改文件但不 commit，不会自动推送
- `.env` 已被 `.gitignore` 忽略，不会上传你的密钥
