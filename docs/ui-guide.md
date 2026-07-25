# AutoCut Web UI 使用指南

## 启动

```shell
pip install autocut-sub[ui]   # 首次需安装 gradio
autocut --ui
```

启动后浏览器自动打开（默认 `http://127.0.0.1:7860`）。

## 使用步骤

界面分为 4 个标签页，按顺序操作即可完成整个剪辑流程。

### 1. 导入视频

上传需要剪辑的视频或音频文件。支持 mp4、mov、mkv、avi、flv、webm、mp3、wav、m4a、flac 格式。

上传后界面会显示文件名确认。

### 2. 生成字幕

有两种方式获取字幕：

**自动转录** — 使用 Whisper 模型从视频中生成字幕：

| 参数 | 说明 |
|---|---|
| 语言 | 视频语言，如 zh（中文）、en（英文） |
| Whisper 模式 | whisper（本地）、faster（更快）、openai（API） |
| 模型大小 | tiny/base/small/medium/large，越大越准但越慢 |
| 设备 | auto（自动）、cpu、cuda（GPU） |

点击「开始转录」后，进度区域会实时显示当前步骤（加载模型 → VAD 检测 → 转录），转录完成后可下载生成的 SRT 文件。

**导入已有字幕** — 如果已有 SRT、MD 或 JSON 文件，直接上传即可跳过转录。

### 3. 编辑片段

点击「加载片段」后，表格中显示所有字幕片段：

| 列 | 说明 |
|---|---|
| Index | 字幕序号 |
| Start | 起始时间（秒） |
| End | 结束时间（秒） |
| Text | 字幕文本 |
| **Keep** | 勾选=保留，不勾=丢弃 |
| Transition | 转场类型（见下方说明） |
| Trans. Duration | 转场时长（秒） |

**转场类型说明：**

| 类型 | 含义 |
|---|---|
| cut | 硬切，直接跳到下一段 |
| fade | 淡入淡出，当前片段淡出至黑屏，下一段淡入 |
| crossfade | 交叉淡入淡出，当前片段淡出同时下一段淡入 |

转场作用在片段的结尾处：设置 `fade` 表示该片段结尾淡出，下一个片段开头淡入。

### 4. 剪辑视频

- 勾选「帧精确剪切（推荐）」可确保切割精度（默认开启）
- 点击「开始剪辑」，进度区域会实时显示当前步骤（提取片段 → 应用转场 → 合并）
- 剪辑完成后显示输出文件路径
- 「保存项目 JSON」可将当前编辑保存为 `.json` 文件，下次直接加载无需重新操作

## 完整工作流示例

```shell
# 方式一：全程使用 UI
autocut --ui
# 1. 导入视频 → 2. 点击转录 → 3. 编辑片段 → 4. 剪辑

# 方式二：CLI 转录 + UI 编辑
autocut -t demo.mp4      # 先用 CLI 生成 SRT
autocut --ui              # 再用 UI 编辑和剪辑
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

工作流中的角色：由转录步骤生成，被编辑和剪切步骤读取。

### MD 编辑文件

Markdown 格式，用于选择保留哪些字幕片段。由转录步骤生成，用户编辑后供剪切步骤读取。

```markdown
- [ ] <-- Mark if you are done editing.

<video controls="true" allowfullscreen="true"> <source src="demo.mp4" type="video/mp4"> </video>

Texts generated from [demo.srt](demo.srt). Mark the sentences to keep for autocut.
The format is [subtitle_index,duration_in_second] subtitle context.

- [ ] [1,00:00]   大家好，这是一条测试视频。
- [x] [2,00:05]   Hello, this is a test video.
```

格式规则：
- 第一行是"完成编辑"标记，用户编辑完成后勾选为 `[x]`，剪切步骤仅在标记完成后执行
- 每条字幕一行，`- [ ]` 表示丢弃，`- [x]` 表示保留
- 每行前缀格式：`[序号,分:秒]`，文本对齐排列
- 视频嵌入标签仅当输入是视频文件时添加

工作流中的角色：由转录步骤生成 → 用户编辑 → 被剪切步骤读取。

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
     ▼  转录（自动或 CLI）
  ┌──────┐
  │ .srt │  字幕时间轴
  │ .md  │  编辑选择文件
  └──────┘
     │
     ▼  用户编辑（UI 表格或 MD 勾选）
  ┌──────┐
  │ .md  │  已编辑
  └──────┘
     │
     ▼  剪切
  ┌──────┐
  │ .json│  项目文件（可复用）
  │ _cut │  输出视频/音频
  └──────┘
```
