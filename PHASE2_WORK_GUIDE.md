# 如我所书：第一阶段后续工作指南

> 本文面向后续子代理。第一阶段核心兼容层位于 `lore.py`，不要复制一套新的 schema 规范；所有保存路径应复用 `normalize_world()`、`normalize_character()` 与 `sync_world_eggs()`。

## 通用约束

- 插件目录：`C:\Users\14476\.astrbot\data\plugins\astrbot_plugin_as_i_write`
- 数据目录：`C:\Users\14476\.astrbot\data\plugin_data\astrbot_plugin_as_i_write`
- 每次修改前完整读取目标文件；只改本文指定文件。
- 不删除 `content`、`easter_eggs`、`eggs`、RPG 属性等旧字段。
- ID 使用 UUID hex 或 `lore.normalize_lore_entry()` 自动生成，不使用数组下标作为永久 ID。
- 写文件继续使用原子的 `_save_json()` 或桥接层 `_save()`。
- 完成后运行 `syntax_check`，并为纯转换函数补 pytest；最后在 AstrBot WebUI 重载 `astrbot_plugin_as_i_write`（即 load_plugin/reload plugin），检查日志无导入异常。

---

## 1. WebUI 世界书条目编辑器

### 目标文件

- `webui_index.html`
- `webui_bridge.py`
- 可复用但原则上无需改动：`lore.py`

### 具体改动

1. 在世界观编辑页加入“世界书条目”区块，数据源为当前 world 的 `entries`。
2. 列表每项显示：`name`、`keys`、`constant`、`enabled`、`priority`、`insertion_order`、`insertion_position`，并提供编辑、删除按钮。
3. 新建/编辑表单完整覆盖：
   - `id`（新建时可留空，保存时自动生成）
   - `name`
   - `keys`（界面可用逗号/换行输入，提交为字符串数组）
   - `secondary_keys`
   - `content`
   - `constant`
   - `enabled`
   - `priority`（整数）
   - `insertion_order`（整数）
   - `insertion_position`（枚举：`system`、`before_char`、`after_char`、`before_history`、`after_history`）
4. 删除必须按 `id` 删除并二次确认；取消编辑不得污染当前保存对象。
5. `webui_bridge.py::_normalize_legacy_world()` 已接入 `normalize_world()`；保存 API 仍走 `PUT /worlds/{name}`。不要另建一套 entries 默认值。
6. 处理旧 world：打开时应立即显示由 `content` 迁移出的“世界观总览”常驻条目；保存后仍保留原 `content`。
7. 保持 WebUI 现有 overview/NPC/剧情线等编辑功能不变。

### 验收标准

- 对旧世界首次打开时可看到一条 `constant=true` 的总览条目。
- 可新建、编辑、删除条目；刷新页面后数据不丢失，ID 稳定。
- 开关能正确保存布尔值；priority/order 保存为整数。
- 五种插入位置均可选择并保存。
- 保存后 `worlds.json` 同时保留 `content`、`entries`、`eggs`、`easter_eggs`。
- 手动创建一个关键词条目，开始冒险并输入关键词时能够命中；禁用后不命中。

---

## 2. Chara Card V3 JSON 导入导出

### 目标文件

- 新建 `chara_card_v3.py`
- `webui_bridge.py`（增加导入/导出 API）
- `webui_index.html`（增加 JSON 文件选择、导入和下载按钮）
- 新建 `test_chara_card_v3.py`

### 具体改动

1. 在 `chara_card_v3.py` 提供纯函数，建议名称：
   - `character_to_ccv3(character: dict) -> dict`
   - `character_from_ccv3(card: dict) -> dict`
   - `world_to_ccv3(world: dict, name: str) -> dict`
   - `world_from_ccv3(card: dict) -> tuple[str, dict]`
2. 接受 V3 常见外壳：`{"spec":"chara_card_v3","spec_version":"3.0","data":{...}}`；导出必须生成同样外壳。
3. 角色字段映射至少覆盖：`name`、`description`、`personality`、`scenario`、`first_mes`、`mes_example`、`system_prompt`、`post_history_instructions`、`tags`、`creator_notes`、`character_version`、`alternate_greetings`、`character_book.entries`。
4. 世界书条目映射覆盖第一阶段条目全部字段。对外部格式的 `key/keys`、`secondary_keys`、`constant`、`enabled`、`priority/order`、`position` 做宽容读取；内部结果最后调用 `normalize_world()` / `normalize_character()`。
5. 插件私有 RPG 字段无法映射时放入 `data.extensions.as_i_write`，导回时恢复，避免角色属性丢失。
6. 世界导出可以使用一个“仅世界书”的 V3 数据对象；必须把 world 的原始 `content` 和兼容字段放入 `extensions.as_i_write.world`，保证本插件往返无损。
7. 桥接层增加明确路由，例如：
   - `POST /import/character`
   - `POST /import/world`
   - `GET /export/character/{uid}`
   - `GET /export/world/{name}`
   响应必须包含清晰错误信息，不允许解析异常变成 500 裸栈。

### 验收标准

- 本插件角色卡导出→导入后，新字段和 RPG 旧字段等价。
- world 导出→导入后，entries 的 ID、关键词、开关、优先级、顺序、位置等价。
- 能读取缺少可选字段的 V3 卡；非法 `spec` 或非 JSON 给出 400。
- 测试至少覆盖角色 round-trip、世界 round-trip、旧字段扩展保留和非法输入。

---

## 3. PNG 角色卡读取

### 目标文件

- 在 `chara_card_v3.py` 增加 PNG 解析函数，或新建 `png_card.py`
- `webui_bridge.py`
- `webui_index.html`
- 新建/扩充 `test_chara_card_v3.py`
- 只读参考样本：`C:\Users\14476\Desktop\崩铁，星海寻图.png`

### 具体改动

1. 按 PNG 规范解析：校验 8 字节签名，逐 chunk 读取 `length(4, big-endian) + type(4) + data + crc(4)`，遇到 `IEND` 停止。
2. 支持 `tEXt`：data 为 `keyword\0text`；优先查找 keyword `ccv3`，其次 `chara`。
3. 建议同时兼容 `zTXt` 与 `iTXt`，但 MVP 验收至少要求 `tEXt`。
4. 文本 payload 常为 base64：先严格 base64 解码，再按 UTF-8 解析 JSON；若 payload 本身以 `{` 开头，也允许直接 JSON。
5. 解出的 JSON 交给第二节 `character_from_ccv3()`，不要在 PNG 模块重复字段映射。
6. 设置上传体积上限（建议 20MB）、chunk 长度上限，拒绝截断文件和越界长度；不执行任何嵌入内容。
7. WebUI 增加 PNG 上传入口，解析预览后由用户确认写入哪个 UID；不得自动覆盖现有角色。

### 验收标准

- 样本 `崩铁，星海寻图.png` 能识别出 `chara_card_v3` 并导入角色主要字段与 character_book。
- `ccv3` 和 `chara` 同时存在时优先 `ccv3`。
- 非 PNG、无角色卡 chunk、坏 base64、坏 JSON、截断 chunk 均返回可读错误。
- 测试自行构造最小 PNG chunk，不依赖桌面样本才能运行。

---

## 4. 独立迁移工具

### 目标文件

- 新建 `migrate_schema_v2.py`
- 复用 `lore.py` 与 `core.py`
- 新建 `test_migrate_schema_v2.py`

### 具体改动

1. 提供可直接执行的 CLI，默认数据目录为 AstrBot 实际数据目录，也允许 `--data-dir` 覆盖。
2. 迁移前分别将 `characters.json`、`worlds.json` 原字节复制为：
   - `characters.json.bak.v1`
   - `worlds.json.bak.v1`
3. 若 `.bak.v1` 已存在，不覆盖；提示用户使用 `--force-backup` 或直接中止。建议默认中止以防覆盖唯一备份。
4. JSON 全部解析成功后再写任何目标文件；使用临时文件 + replace 原子落盘。
5. 调用 `normalize_collections()` 完成升级，确保每个实体有 `schema_version`，旧 content 迁移为常驻 entries，角色字段补齐，彩蛋双字段同步。
6. 支持 `--dry-run`：只输出角色数、世界数、将新增条目数、会变更文件，不写备份和数据。
7. 迁移完成后重新读回并校验 JSON、版本、条目 ID 唯一性；失败则自动恢复 `.bak.v1`。

### 验收标准

- 在临时目录运行，原文件与 `.bak.v1` 哈希完全一致。
- 迁移后所有旧字段仍在，新增字段完整，连续运行第二次数据不再变化。
- 任何一个输入 JSON 损坏时，两份目标文件都不得改变。
- dry-run 不创建文件。

---

## 5. 正则脚本支持

### 目标文件

- 新建 `render_pipeline.py`
- `main.py`（仅在 LLM 输出发送给用户前调用）
- `_conf_schema.json` 或独立数据文件 `regex_scripts.json`
- `webui_bridge.py` / `webui_index.html`（若本轮同时做配置 UI）
- 新建 `test_render_pipeline.py`

### 具体改动

1. 脚本结构至少为：
   ```json
   {
     "id": "uuid",
     "name": "脚本名",
     "enabled": true,
     "find_regex": "正则",
     "replace_string": "替换文本"
   }
   ```
2. 实现 `apply_render_scripts(text: str, scripts: list[dict]) -> str`，按数组顺序对启用脚本调用 `re.sub`。
3. 只处理“LLM completion_text → 用户显示”的副本；写入 `game_state.history` 的内容必须保留原始 LLM 文本，防止存档和后续上下文被展示规则污染。
4. 覆盖单人开场、单人推进、群体场景和群体线索的用户显示路径；普通指令文本、用户输入、世界书内容不处理。
5. 单条非法正则不能中断回复：记录 warning，跳过该条。设置脚本数、正则长度、输入/输出长度上限；避免灾难性回溯，条件允许可使用支持 timeout 的 regex 库，否则限制表达式并在文档中说明。
6. 替换语法明确采用 Python `re.sub` 规则（如 `\\1` 反向引用），WebUI 中给出示例和实时预览。

### 验收标准

- `enabled=false` 完全不生效；多条规则按顺序执行。
- 非法正则只跳过自身，用户仍收到回复。
- 历史中保存原文，用户看到替换后文本。
- 单人和群体全部显示路径一致生效。
- 测试覆盖普通替换、捕获组、禁用、非法表达式、顺序性、长度上限。
