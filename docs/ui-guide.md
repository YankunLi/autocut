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
| Transition | 转场类型，目前仅 cut 生效（见下方说明） |
| Trans. Duration | 转场时长（秒），fade/crossfade 时使用 |

**转场类型说明：**

| 类型 | 含义 |
|---|---|
| cut | 硬切，直接跳到下一段，无过渡效果（当前默认且唯一生效的方式） |
| fade | 淡入淡出，当前片段逐渐淡出至黑屏/静音，下一段从黑屏/静音淡入 |
| crossfade | 交叉淡入淡出，当前片段淡出的同时下一段淡入，两段内容短暂重叠混合 |

> 注意：fade 和 crossfade 为预留选项，当前版本尚未实现，剪切时统一按 cut 处理。

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
