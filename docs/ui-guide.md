# AutoCut Web UI 使用指南

## 启动

```shell
pip install autocut-sub[ui]   # 首次需安装 gradio
autocut --ui
```

启动后浏览器自动打开（默认 `http://127.0.0.1:7860`）。

## 使用步骤

### 1. 加载片段

- 在 "Media file path" 输入视频文件路径（如 `D:\videos\demo.mp4`）
- 上传 SRT 字幕文件 **或** JSON 项目文件
- 如果有 MD 编辑文件，也可以一并上传（SRT + MD 组合）
- 点击 **Load** 按钮，下方表格会显示所有字幕片段

### 2. 编辑片段

表格每行一个片段，列含义：

| 列 | 说明 |
|---|---|
| Index | 字幕序号（只读） |
| Start | 起始时间（秒） |
| End | 结束时间（秒） |
| Text | 字幕文本 |
| **Keep** | 勾选=保留，不勾=丢弃 |
| Transition | 转场类型（见下方说明） |
| Trans. Duration | 转场时长（秒），fade/crossfade 时使用 |

**转场类型说明：**

| 类型 | 含义 |
|---|---|
| cut | 硬切，直接跳到下一段，无过渡效果 |
| fade | 淡入淡出，当前片段逐渐淡出至黑屏/静音，下一段从黑屏/静音淡入 |
| crossfade | 交叉淡入淡出，当前片段淡出的同时下一段淡入，两段内容短暂重叠混合 |

转场作用在片段的结尾处：设置 `fade` 表示该片段结尾淡出，下一个片段开头淡入。

直接在表格中编辑：勾选要保留的片段、调整时间、选择转场。

### 3. 执行操作

- **Save Project JSON** — 将当前编辑保存为 `.json` 文件，下次可用 JSON 文件直接加载，无需重新上传 SRT
- **Run Cut** — 执行剪切，输出 `_cut.mp4` 文件
- **Precise** 复选框 — 帧精确剪切（默认开启，对非关键帧位置重编码以确保精度；关闭则使用流复制，速度更快但切割点可能偏移）

## 完整工作流示例

```shell
# 1. 先用 CLI 转录视频生成 SRT
autocut -t demo.mp4

# 2. 启动 UI 编辑
autocut --ui

# 3. 在 UI 中：输入 demo.mp4 路径 → 上传 demo.srt → Load
# 4. 勾选要保留的片段
# 5. 点击 Run Cut 执行剪切
# 6. 保存 JSON 以便下次编辑
```

## 文件格式说明

AutoCut 使用三种文件格式在转录、编辑和剪切步骤之间传递数据：

### SRT 字幕文件

标准 SubRip 格式，由转录步骤生成。

```
1
00:00:00,000 --> 00:00:05,000
大家好，这是一条测试视频。

2
00:00:05,000 --> 00:00:10,260
Hello, this is a test video.
```

格式规则：
- 每条字幕包含序号、时间轴、文本，条目之间用空行分隔
- 时间格式：`HH:MM:SS,mmm`（时:分:秒,毫秒）
- 语音间隔超过 1 秒时，自动插入 `< No Speech >` 条目
- 还有一种紧凑格式（`-s` 参数生成），每条字幕占一行：`00:00:00,000 --> 00:00:05,000 字幕文本`

工作流中的角色：由 `autocut -t` 生成，被编辑和剪切步骤读取。

### MD 编辑文件

Markdown 格式，用于选择保留哪些字幕片段。由转录步骤生成，用户编辑后供剪切步骤读取。

```markdown
- [ ] <-- Mark if you are done editing.

<video controls="true" allowfullscreen="true"> <source src="demo.mp4" type="video/mp4"> </video>

Texts generated from [demo.srt](demo.srt).Mark the sentences to keep for autocut.
The format is [subtitle_index,duration_in_second] subtitle context.

- [ ] [1,00:00]   大家好，这是一条测试视频。
- [x] [2,00:05]   Hello, this is a test video.
```

格式规则：
- 第一行是"完成编辑"标记，用户编辑完成后勾选为 `[x]`，剪切步骤仅在标记完成后执行
- 每条字幕一行，`- [ ]` 表示丢弃，`- [x]` 表示保留
- 每行前缀格式：`[序号,分:秒]`，文本对齐排列
- 视频嵌入标签仅当输入是视频文件时添加

工作流中的角色：由 `autocut -t` 生成 → 用户编辑 → 被 `autocut -c` 读取。

### JSON 项目文件

结构化 JSON 格式，保存完整的剪辑项目信息，包含所有片段及其保留状态和转场设置。

```json
{
  "version": "1.0",
  "source": "demo.mp4",
  "segments": [
    {
      "index": 1,
      "start": 0.0,
      "end": 5.0,
      "text": "大家好，这是一条测试视频。",
      "keep": true,
      "transition": "fade",
      "transition_duration": 0.5
    },
    {
      "index": 2,
      "start": 5.0,
      "end": 10.26,
      "text": "Hello, this is a test video.",
      "keep": false,
      "transition": "cut",
      "transition_duration": 0.0
    }
  ]
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string | 版本号，固定 `"1.0"` |
| `source` | string | 源媒体文件路径 |
| `segments` | array | 片段列表 |
| `segments[].index` | int | 字幕序号（从 SRT 继承，1-based） |
| `segments[].start` | float | 起始时间（秒） |
| `segments[].end` | float | 结束时间（秒） |
| `segments[].text` | string | 字幕文本 |
| `segments[].keep` | bool | 是否保留该片段 |
| `segments[].transition` | string | 片段结尾的转场类型：`cut` / `fade` / `crossfade` |
| `segments[].transition_duration` | float | 转场时长（秒） |

工作流中的角色：由 Web UI 的 Save 或剪切步骤自动生成，可被 Web UI 和 CLI 直接加载，无需再次上传 SRT/MD。

### 文件流转关系

```
视频/音频文件
     │
     ▼  转录 (autocut -t)
  ┌──────┐
  │ .srt │  字幕时间轴
  │ .md  │  编辑选择文件
  └──────┘
     │
     ▼  用户编辑 .md（勾选保留片段，标记完成编辑）
  ┌──────┐
  │ .md  │  已编辑
  └──────┘
     │
     ▼  剪切 (autocut -c)
  ┌──────┐
  │ .json│  项目文件（可复用）
  │ _cut │  输出视频/音频
  └──────┘
```
