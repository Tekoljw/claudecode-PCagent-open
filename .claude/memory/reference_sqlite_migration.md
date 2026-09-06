---
name: 灯光库从平文件迁移到 SQLite 的历史
description: 2026-09 曾经用 light_library_memory.json 存灯光库，后改成本地 SQLite（light_library.db），protocol 消息类型名字没变，只是内部实现换了。
metadata:
  type: reference
---

早期版本（`write_memory`/`read_memory` WS 消息刚加的时候）灯光库数据直接存
`light_library_memory.json` 平文件，`content` 原样 JSON dump/load。后来因为
claude-code-AI 那边要做"云端只读缓存 + PC 离线也能看"，需要更结构化的存储
（尤其是图片要能按槽位单独更新/清空），改成了 SQLite（`light_library.db`，
两张表 `light_library` 单例行 + `light_reference_media` 5 槽位 BLOB）。

**对外协议完全没变**——`write_memory`/`read_memory`/`memory_content`/
`memory_write_result` 这几个 WS 消息类型名字、字段形状（`{exists, content,
error}` / `{ok, error}`）都保持原样，`content` 里的 JSON 结构
（`generatedAt`/`promptForClaudeCode`/`venueFixtures`/`venueReferenceMedia`）
也没变。改的只是 `read_light_memory()`/`write_light_memory()` 内部怎么把这份
JSON 落到磁盘上——这意味着以后如果要再换存储方式（比如真的数据量大到 SQLite
不够用的那天），只要保持这两个函数的返回/参数形状不变，`agent-server.js`/
`pc-mcp-server.js` 那边不需要跟着改。

`_data_url_to_bytes`/`_bytes_to_data_url` 是这次迁移新加的两个转换函数——图片
在协议层仍然是 `data:image/jpeg;base64,...` 这种 dataUrl 字符串（浏览器原生格式），
只有落盘到 SQLite 时才解成原始字节存 BLOB（省 base64 膨胀的 33% 体积），读出来
时再编回 dataUrl 字符串。`video30s` 槽位从设计上就没有走这条路径——视频从来
不内嵌 dataUrl，只存 `label`/`fileName`/`note` 元信息。
