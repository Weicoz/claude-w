# claude-w

macOS 上的 Claude Code 额度窗口管理工具：定时发起最小 Haiku 请求、查看额度缓存和真实请求历史、终止 Claude 相关进程。只使用 Python 标准库。

## 安装

需要 macOS、Python 3.9+、已登录订阅账号的 Claude Code，以及 Bettbox 的 TUN 模式。本工具沿用现有 Bettbox 固定出口检查，并非通用代理管理器。

```bash
git clone https://github.com/Weicoz/claude-w.git
cd claude-w
python3 install.py --expected-ip <你的固定出口IPv4>
```

将 `~/.local/bin` 加入 PATH。已有 `claude-window` 安装可直接运行 `python3 install.py`，自动保留原固定出口设置并迁入旧请求结果；已有计划、状态和 LaunchAgent 不覆盖。安装前自动备份，不自动启动任务，也不发送请求。命令链接到当前仓库，请保留仓库路径。

## 使用

```bash
claude-w status                 # 任务、额度、上次真实发起及成功完成时间
claude-w history                # 最近 20 次真实请求与结果
claude-w history --limit 50
claude-w usage                  # Claude Code 状态栏提供的额度缓存
claude-w logs                   # 最近 20 行执行日志
claude-w 10:00                  # 安排下一次可执行的 10:00 目标重置时间
claude-w 10:00 -n 3             # 连续 3 个目标日期
claude-w 10:00 -d               # 每天重复
claude-w cancel                 # 取消计划并停止任务
claude-w auto                   # 恢复周期自动模式并启动任务
claude-w start                  # 启用定时检查
claude-w stop                   # 停止任务，后续登录也不自动启动
claude-w check --dry-run        # 检查是否可发送，本次不调用 Claude
claude-w poke                   # 立即尝试最小请求，仍受出口、额度等保护
claude-w kill --dry-run         # 预览终止范围
claude-w kill                  # 强制终止；未保存内容可能丢失
```

`ckill` 和 `claude-w ckill` 都转入同一终止实现。匹配当前用户完整命令行中包含 `claude` 的进程，排除工具自身及其祖先进程；范围包括 App、Code，也可能包括相关辅助进程。`kill` 不禁用定时任务，若需要停止自动请求请用 `stop`。

## 时间与历史的含义

- **真实发起**：成功创建 Claude Code 请求进程后立即记录的本机时间；即使随后失败或被终止，仍保留发起记录。跳过检查、模拟检查、创建计划不记为真实发起。
- **成功完成**：请求进程返回成功且返回有效结果；不能单凭此时间断言服务器首次收到请求或额度重置的精确时刻。
- 旧版本只记录成功/失败的完成时间。安装迁入这些记录时明确标为旧记录，发起时间未知，不按耗时反推。
- `history` 只统计本工具发起的请求，不读取用户手动 Claude 会话或聊天正文。历史保存在 `~/.local/state/claude-window/requests.jsonl`，权限 `0600`，不随执行日志轮转。
- 如果发起后进程被强制终止且没记录结果，历史显示“已发起，结果未记录”。

`HH:MM` 表示目标重置时间，工具反推 5 小时安排请求；每 10 分钟检查一次，所以时间近似。睡眠导致错过超过 20 分钟、已有活跃窗口时跳过，不补发。

## 保护与本地数据

未配置固定出口、Bettbox TUN 关闭、路由异常、`api.anthropic.com/cdn-cgi/trace` 出口不匹配、订阅 OAuth 未就绪时，均不调用 Claude。失败退避 1 小时；近 24 小时最多 4 次成功请求；周额度已用 95% 时跳过。最小请求会消耗订阅额度。

```bash
claude-w config --expected-ip <你的固定出口IPv4>
```

配置、运行状态、历史和备份均位于 `~/.local/state/claude-window/`；执行日志位于 `~/Library/Logs/claude-window.jsonl`。这些数据不提交 Git。额度读取 `~/.claude/rl-cache.json`，需要已有 Claude Code 状态栏集成提供 `rate_limits.five_hour` / `rate_limits.seven_day`，本工具不安装或覆盖状态栏配置。没有缓存时周期模式会等待，`poke` 可尝试首次激活。

## 验证

```bash
python3 -m unittest -v test_claude_w.py
```

测试使用临时目录和模拟进程，不调用 Claude、不杀真实进程、不改实际 LaunchAgent。
