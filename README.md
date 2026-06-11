# B站直播弹幕插件

这个插件用于读取 Bilibili 直播间弹幕事件，并把弹幕、礼物、进房等事件交给 Yuyu-Mind 的事件工作流。桌宠可以根据弹幕内容在本地气泡和语音中回应观众。

## 启用方式

编辑：

```text
agent/plugins/bilibili_live/config.json
```

最小配置：

```json
{
  "enabled": true,
  "roomId": 123456,
  "mentionNames": ["Yuyu", "鱼鱼", "桌宠"],
  "onlyReplyMentions": true,
  "maxEventsPerPoll": 5,
  "maxRepliesPerMinute": 4,
  "speakPriority": 60,
  "replyToGifts": true,
  "replyToEnter": false,
  "blockedWords": [],
  "cookie": "",
  "cookieFile": "cookie.txt",
  "timeoutSeconds": 12
}
```

保存后重启 Agent 后端。

## 配置说明

- `enabled`: 是否启用直播间连接。
- `roomId`: Bilibili 直播间房间号，支持短号，插件会自动解析真实房间号。
- `mentionNames`: 弹幕中出现这些名字时视为点名桌宠。
- `onlyReplyMentions`: 为 `true` 时只回复点名弹幕和高优先级事件，适合直播间弹幕较多的场景。
- `maxRepliesPerMinute`: 每分钟最多主动回复多少条普通弹幕。
- `speakPriority`: 事件优先级达到该值时允许语音播报。礼物默认优先级更高。
- `replyToGifts`: 是否回应礼物。
- `replyToEnter`: 是否回应进房事件。直播间人数多时建议保持关闭。
- `blockedWords`: 命中这些词的弹幕会被忽略。
- `cookie`: 可选的 Bilibili 浏览器 Cookie。因为 Cookie 里可能包含 JSON 特殊字符，更推荐使用 `cookieFile`。
- `cookieFile`: 可选的 Cookie 文本文件路径。相对路径会从插件目录解析，例如 `cookie.txt`。
- `timeoutSeconds`: 连接 Bilibili API 和弹幕服务器的超时时间。

如果匿名接口返回 `-352`，建议在 `agent/plugins/bilibili_live/cookie.txt` 中粘贴完整 Cookie 原文，然后在配置里设置 `"cookieFile": "cookie.txt"`。

## 当前边界

插件目前只读取弹幕事件，不会替你向 Bilibili 直播间发送弹幕，也不需要登录 Cookie。它会把事件交给桌宠本地回复，因此观众能否听到取决于你的直播推流是否采集了桌宠声音。

Bilibili 的弹幕协议可能变动；如果状态中持续出现连接错误，先确认房间号正确、网络能访问 Bilibili，再重启 Agent。

## 弹幕调用能力

直播弹幕会复用完整模式里的插件匹配规则。观众弹幕中带有对应关键词时，桌宠会先执行插件任务，再基于结果回复。

示例：

```text
搜一下今天有什么 AI 新闻
看一下屏幕上是什么报错
网易云播放一首轻松的歌
```

为了避免每条弹幕都触发耗时任务，只有命中搜索、读屏、音乐等插件关键词时才会调用对应插件。
