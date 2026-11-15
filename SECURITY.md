# 安全说明

## account_token 等于账号凭证

工具需要从雷神客户端日志里读出 `account_token` 来调用官方 pause 接口。这个 token
**等同于你的加速凭证**：拿到它的人可以暂停/恢复你的加速，不需要密码。

所以：

- 日志、`config.json`、`.token_cache` 都已在 `.gitignore` 里，**别提交进仓库**；
- 贴 issue 之前先检查，`2a3G***109` 之外的长串都要抹掉（`status` 输出是自动脱敏的，可以直接贴）；
- GitHub Actions 的每日兜底只把 token 放 **Repository secret**，不要写在 workflow 文件里。

## 报告漏洞

如果发现能导致 token 泄露、命令注入（比如日志内容被当成命令执行）之类的问题，
请用 GitHub 的 **Security → Report a vulnerability**（私密 advisory），不要开公开 issue。
