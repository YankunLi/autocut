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
| Transition | 转场类型（cut/fade/crossfade） |
| Trans. Duration | 转场时长（秒） |

直接在表格中编辑：勾选要保留的片段、调整时间、选择转场。

### 3. 执行操作

- **Save Project JSON** — 将当前编辑保存为 `.json` 文件，下次可用 JSON 文件直接加载，无需重新上传 SRT
- **Run Cut** — 执行剪切，输出 `_cut.mp4` 文件
- **Precise** 复选框 — 勾选后使用帧精确剪切（较慢，在非关键帧位置重编码）

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
